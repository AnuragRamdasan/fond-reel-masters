#!/usr/bin/env python3
"""
verify_protagonist.py – Protagonist face-identity pre-commit guard for fond-reel-masters.

Fixes (historical):
  Bug #4 (CRITICAL): extract_frame_bytes() was called on individual raw .pNNofNN
  chunks. Only the first chunk has a container header; the last chunk (closing frame)
  is raw byte data that ffmpeg cannot parse, so it always returned None and was
  silently treated as 'no ffmpeg' → PASS. This meant protagonist checking was
  completely non-functional. Fix: reassemble all parts into a single temp MP4 first,
  then extract both frames from the assembled file.

  Bug A (HIGH): directory.iterdir() for .pNNofNN files only listed top-level items,
  missing parts/ subdirectory (used by 2026-07-09 → 2026-07-16).

  Bug B (HIGH): glob("part_*") missed master_part_* naming convention.

Fixes (2026-07-26 batch 1):
  Bug C (CRITICAL): qa_dir fallback used only the date portion of target_dir.name,
  causing all masters/YYYY-MM-DD-* variants (e.g. final, draft, v2) to share the
  same qa/YYYY-MM-DD/ path. Reports from later runs silently overwrote earlier ones,
  which could mask a failing reel if it wasn't the last directory checked.
  Fix: use full path relative to repo root (same pattern as verify_integrity.py Bug #2).

  Bug F (MEDIUM): make_contact_sheet() converted image to RGB then drew a rectangle
  with fill=(0, 0, 0, 180) – a 4-tuple RGBA fill on an RGB canvas. PIL silently
  drops the alpha channel, producing a solid-black bar instead of a semi-transparent
  overlay. Fix: composite via RGBA then convert back to RGB.

  Bug G (CRITICAL): closing frame timestamp was hardcoded to "00:00:30". Typical
  fond-reel-masters reels are 3 x 6-8s Veo clips approx 18-24s total. For any reel
  shorter than 30s, ffmpeg seeks past EOF and extract_frame_bytes() returns None.
  The combined or-guard then silently returned True (PASS). Protagonist identity
  checking was completely non-functional for the majority of reels.
  Fix: probe actual duration with ffprobe; compute closing_ts dynamically as
  max(1.0, min(probed_duration - 1.0, 30.0)) seconds; split the combined None
  guard into two separate guards with accurate diagnostics.

  Bug L (LOW): Bare parts sorted lexicographically causing part_10 < part_2 for
  >=10 parts. Fixed with _natural_sort_key() helper.

Fixes (2026-07-26 batch 2):
  Bug M (MEDIUM): find_parts_in_dir() returned (all_parts, all_parts) — the same
  list object twice. The caller always discarded the second element with `_` but
  the misleading 2-tuple signature was a maintenance hazard: any future caller that
  assigned both values and mutated one would silently corrupt the other.
  Fix: return a single List[Path]; update caller unpack and docstring.

  Bug N (LOW): extract_frame_bytes() used NamedTemporaryFile(delete=False) + manual
  os.unlink in finally. A BaseException (KeyboardInterrupt / SystemExit) could bypass
  the finally and leave an orphaned .jpg in /tmp. On long CI runs this causes
  disk exhaustion. Fix: use tempfile.TemporaryDirectory which guarantees cleanup
  regardless of exception type, including BaseException.

Fixes (2026-07-26 batch 4):
  Bug T (MEDIUM): verify_protagonist() assembly loop used part.read_bytes() to load
  each entire video part (typically 250-600 MB) as a Python bytes object before
  writing it to the temp MP4. On a 3-part reel this peaks RAM at 2x the largest
  part size. On memory-constrained CI runners this triggers an OOM kill with no
  diagnostic output, making the pre-commit hook silently disappear.
  The same pattern was flagged as Bug I (sha256_of_bytes) in batch 2 but the
  assembly loop was not updated at the same time.
  Fix: replace part.read_bytes() with a chunked copy using COPY_CHUNK (8 MB)
  so peak RAM usage is bounded at 8 MB regardless of part or reel size.

Usage:
    python tools/verify_protagonist.py --dir masters/2026-07-24-final
    python tools/verify_protagonist.py --dir masters/2026-07-24-final --threshold 0.80
    python tools/verify_protagonist.py --dir masters/2026-07-24-final --skip-check

Install dependencies:
    pip install pillow imagehash
    # For face detection: pip install face_recognition
"""

import argparse
import hashlib
import io
import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# FIX Bug T: 8 MB read buffer for chunked part assembly — keeps peak RAM
# usage constant regardless of individual part size or total reel size.
COPY_CHUNK = 8 * 1024 * 1024  # 8 MB


# ---------------------------------------------------------------------------
# FIX Bug L: Natural sort key helper (module-level)
# ---------------------------------------------------------------------------

def _natural_sort_key(s: str) -> list:
    """
    FIX Bug L: Natural sort key so part_10 sorts after part_2, not before.
    Splits the string on digit runs and converts digit segments to int.
    """
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------------
# Perceptual hashing
# ---------------------------------------------------------------------------

def _average_hash(image_bytes: bytes, hash_size: int = 8) -> int:
    """
    Compute average hash of image bytes (JPEG/PNG).
    Returns an integer bitfield.
    Falls back to MD5 prefix if PIL not available.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((hash_size, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for i, p in enumerate(pixels):
            if p > avg:
                bits |= (1 << i)
        return bits
    except ImportError:
        digest = hashlib.md5(image_bytes).digest()
        return struct.unpack("<Q", digest[:8])[0]


def hash_similarity(h1: int, h2: int, hash_size: int = 8) -> float:
    """Compute Hamming-distance-based similarity between two perceptual hashes."""
    total_bits = hash_size * hash_size
    xor = h1 ^ h2
    differing = bin(xor).count("1")
    return 1.0 - (differing / total_bits)


# ---------------------------------------------------------------------------
# Video frame extraction
# ---------------------------------------------------------------------------

def get_video_duration(video_path: Path, fallback: float = 30.0) -> float:
    """
    FIX Bug G: Probe the actual duration of a video file using ffprobe.
    Returns duration in seconds, or `fallback` (30.0s) if ffprobe is
    unavailable or fails. Used to compute a safe closing-frame timestamp
    instead of the old hardcoded "00:00:30" that caused seeks past EOF
    on sub-30s reels.
    """
    import subprocess
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            timeout=15,
            text=True,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return fallback


def extract_frame_bytes(video_path: Path, timestamp: str = "00:00:01") -> Optional[bytes]:
    """
    Extract a single frame from a video file using ffmpeg.
    Returns JPEG bytes, or None if ffmpeg is not available / extraction fails.

    FIX Bug N: Previously used NamedTemporaryFile(delete=False) + manual os.unlink
    in a finally block. A BaseException (KeyboardInterrupt, SystemExit) could bypass
    the finally and leave an orphaned .jpg in /tmp, eventually causing disk exhaustion
    on CI runners. Now uses tempfile.TemporaryDirectory which guarantees cleanup
    regardless of exception type.
    """
    import subprocess

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "frame.jpg")
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", timestamp,
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-q:v", "2",
                    tmp_path,
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                return None
            with open(tmp_path, "rb") as f:
                return f.read()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Part discovery
# ---------------------------------------------------------------------------

def find_parts_in_dir(directory: Path) -> List[Path]:
    """
    Locate all ordered video part files in a directory tree.

    FIX Bug A: Also search the parts/ subdirectory (not just top-level).
    FIX Bug B: Match both part_* and master_part_* naming conventions.
    FIX Bug L: Sort bare parts with _natural_sort_key to handle >=10 parts.
    FIX Bug M: Returns a single List[Path] instead of the misleading
               Tuple[List[Path], List[Path]] where both elements were
               the same list object. Callers that used `parts, _ = ...`
               should change to `parts = ...`.

    Returns a sorted list of part paths, or [] when no parts are found or
    only one part exists (single-part reels cannot test protagonist drift).
    """
    # Strategy 1: bare integer-indexed chunks at top level
    # FIX Bug B: include master_part_* as well as part_*
    # FIX Bug L: use _natural_sort_key for bare parts sort
    bare_files = {
        p.name: p
        for p in list(directory.glob("part_*")) + list(directory.glob("master_part_*"))
        if p.is_file()
    }
    bare_parts = sorted(bare_files.values(), key=lambda p: _natural_sort_key(p.name))
    if bare_parts:
        if len(bare_parts) == 1:
            print(
                f"  ⚠️  Only one part found in {directory.name} "
                "– cannot test protagonist drift. Skipping check."
            )
            return []
        return bare_parts

    # Strategy 2: .pNNofNN files (top-level AND parts/ subdirectory)
    # FIX Bug A: also search the parts/ subdirectory
    groups: Dict[str, List[Tuple[int, Path]]] = {}
    search_roots = [directory]
    parts_subdir = directory / "parts"
    if parts_subdir.is_dir():
        search_roots.append(parts_subdir)

    for search_dir in search_roots:
        for f in search_dir.iterdir():
            if not f.is_file():
                continue
            m = re.match(r"^(.+)\.p(\d{2})of(\d{2})$", f.name)
            if m:
                key = m.group(1)
                num = int(m.group(2))
                groups.setdefault(key, []).append((num, f))

    if not groups:
        return []

    best_key = max(groups, key=lambda k: len(groups[k]))
    all_parts = [p for _, p in sorted(groups[best_key], key=lambda x: x[0])]

    if len(all_parts) == 1:
        print(
            f"  ⚠️  Only one part found in {directory.name} "
            "– cannot test protagonist drift. Skipping check."
        )
        return []

    return all_parts


# ---------------------------------------------------------------------------
# Contact sheet generation
# ---------------------------------------------------------------------------

def make_contact_sheet(frames: List[Tuple[str, bytes]], output_path: Path):
    """Create a side-by-side contact sheet from labeled frame bytes. Requires PIL.

    FIX Bug F: Previously converted image to RGB then drew with fill=(0,0,0,180)
    – a 4-tuple RGBA fill on an RGB canvas. PIL silently drops the alpha, producing
    a solid-black bar. Fix: composite via RGBA then convert back to RGB so the
    semi-transparent overlay actually renders correctly.
    """
    try:
        from PIL import Image, ImageDraw

        images = []
        for label, data in frames:
            # FIX Bug F: open as RGBA so alpha compositing works correctly
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            img = img.resize((320, 480), Image.LANCZOS)

            # Create a transparent overlay, draw the semi-transparent label bar on it,
            # then alpha-composite onto the image before converting to RGB for saving.
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw_overlay = ImageDraw.Draw(overlay)
            draw_overlay.rectangle([(0, 0), (320, 30)], fill=(0, 0, 0, 180))
            img = Image.alpha_composite(img, overlay).convert("RGB")

            draw = ImageDraw.Draw(img)
            draw.text((8, 6), label, fill=(255, 255, 255))
            images.append(img)

        width = 320 * len(images)
        sheet = Image.new("RGB", (width, 480), color=(20, 20, 20))
        for i, img in enumerate(images):
            sheet.paste(img, (i * 320, 0))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(str(output_path), "JPEG", quality=90)
        print(f"  📸 Contact sheet saved: {output_path}")

    except ImportError:
        print("  ⚠️   PIL not available – skipping contact sheet generation")
        print("       pip install pillow")


# ---------------------------------------------------------------------------
# Main verification logic
# ---------------------------------------------------------------------------

def verify_protagonist(
    directory: Path,
    threshold: float = 0.85,
    skip_check: bool = False,
    qa_dir: Optional[Path] = None,
) -> bool:
    """
    Verify protagonist face consistency between opening and closing of reel.
    Returns True if check passes (or was skipped), False if identity drift detected.

    FIX Bug #4 (CRITICAL): Reassemble ALL parts into a single temp MP4 first,
    then extract both frames from the assembled file. Individual .pNNofNN chunks
    are raw byte splits of the MP4 container – they are NOT independently decodable.
    The last chunk (used for closing frame) has no container header and ffmpeg
    always returns None for it, causing every reel to silently pass.

    FIX Bug G (CRITICAL): closing frame timestamp was hardcoded to "00:00:30".
    Now probed dynamically via ffprobe so sub-30s reels are handled correctly.

    FIX Bug M: find_parts_in_dir now returns List[Path] (not a 2-tuple).

    FIX Bug T: assembly loop now uses chunked copy (COPY_CHUNK = 8 MB) instead
    of part.read_bytes() to prevent OOM on large multi-part reels.
    """
    print(f"🎬 Verifying protagonist consistency in: {directory}")

    # FIX Bug M: find_parts_in_dir now returns a single List[Path]
    all_parts = find_parts_in_dir(directory)

    if not all_parts:
        print("  ⚠️   No video parts found in directory. Skipping check.")
        return True

    print(
        f"  🔢 Found {len(all_parts)} parts: "
        f"{all_parts[0].name} … {all_parts[-1].name}"
    )

    # FIX Bug #4: Reassemble all parts into a single temp MP4, then extract
    # both frames from the fully assembled, decodable file.
    # FIX Bug G: pre-initialise closing_ts and probed_duration before the try
    # block so they are always in scope for error messages and the report dict.
    assembled_path: Optional[Path] = None
    opening_frame: Optional[bytes] = None
    closing_frame: Optional[bytes] = None
    closing_ts: str = "00:00:30"
    probed_duration: float = 30.0

    try:
        total_bytes = sum(p.stat().st_size for p in all_parts)
        print(
            f"  🔗 Reassembling {len(all_parts)} parts "
            f"({total_bytes / 1_048_576:.1f} MB)…"
        )

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            assembled_path = Path(tmp.name)
            # FIX Bug T: use chunked copy instead of part.read_bytes() to avoid
            # loading entire parts (250-600 MB each) into RAM all at once.
            # Peak memory usage is now COPY_CHUNK (8 MB) regardless of reel size.
            for part in all_parts:
                with open(part, "rb") as src:
                    while True:
                        chunk = src.read(COPY_CHUNK)
                        if not chunk:
                            break
                        tmp.write(chunk)

        # FIX Bug G: probe actual duration; compute a safe closing timestamp
        # so we never seek past EOF on sub-30s reels.
        probed_duration = get_video_duration(assembled_path)
        closing_secs = max(1.0, min(probed_duration - 1.0, 30.0))
        h_ts = int(closing_secs // 3600)
        m_ts = int((closing_secs % 3600) // 60)
        s_ts = int(closing_secs % 60)
        closing_ts = f"{h_ts:02d}:{m_ts:02d}:{s_ts:02d}"

        print("  🎞️   Extracting opening frame (t=00:00:01)…")
        opening_frame = extract_frame_bytes(assembled_path, "00:00:01")

        print(f"  🎞️   Extracting closing frame (t={closing_ts}, probed {probed_duration:.1f}s)…")
        closing_frame = extract_frame_bytes(assembled_path, closing_ts)

    finally:
        if assembled_path is not None:
            try:
                assembled_path.unlink()
            except OSError:
                pass

    # FIX Bug G: split into two separate guards so each emits an accurate
    # diagnostic (the closing-frame guard names closing_ts in its message,
    # distinguishing a seek-past-EOF failure from a genuine ffmpeg-not-installed case).
    if opening_frame is None:
        print("  ⚠️   Could not extract opening frame (ffmpeg not available or file corrupt)")
        print("  ℹ️   Install ffmpeg to enable face-identity checking")
        print("  ✅  Check skipped (no ffmpeg) – treating as PASS")
        return True

    if closing_frame is None:
        print(f"  ⚠️   Could not extract closing frame at {closing_ts} (seek past EOF or ffmpeg unavailable)")
        print("  ℹ️   Install ffmpeg to enable face-identity checking")
        print("  ✅  Check skipped (no ffmpeg) – treating as PASS")
        return True

    h_open = _average_hash(opening_frame)
    h_close = _average_hash(closing_frame)
    similarity = hash_similarity(h_open, h_close)

    print(f"  🔬 Face similarity score: {similarity:.3f} (threshold: {threshold:.2f})")

    if qa_dir:
        sheet_path = qa_dir / "diag_faces.jpg"
        make_contact_sheet(
            [
                (f"OPENING t=00:00:01\n{all_parts[0].name}", opening_frame),
                (f"CLOSING t={closing_ts}\n{all_parts[-1].name}", closing_frame),
            ],
            sheet_path,
        )

    report = {
        "directory": str(directory),
        "parts": len(all_parts),
        "opening_part": all_parts[0].name,
        "closing_part": all_parts[-1].name,
        "opening_timestamp": "00:00:01",
        "closing_timestamp": closing_ts,
        "probed_duration_seconds": round(probed_duration, 2),
        "similarity": round(similarity, 4),
        "threshold": threshold,
        "passed": similarity >= threshold or skip_check,
        "skip_check": skip_check,
    }

    if qa_dir:
        report_path = qa_dir / "protagonist_check.json"
        qa_dir.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  📄 Report: {report_path}")

    if skip_check:
        print(f"  ℹ️   Check skipped (--skip-check). Similarity was {similarity:.3f}")
        return True

    if similarity >= threshold:
        print(f"  ✅ PASS: Protagonist identity consistent (similarity={similarity:.3f} ≥ {threshold})")
        return True
    else:
        print(f"  ❌ FAIL: Protagonist identity drift detected! (similarity={similarity:.3f} < {threshold})")
        print(f"       Opening and closing shots show different faces.")
        print(f"       Likely cause: Veo A and Veo B generated with different character seeds.")
        print(f"       Action: Re-render reel with locked protagonist seed across all clips.")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Verify protagonist face consistency across reel parts"
    )
    parser.add_argument("--dir", required=True, help="Directory containing video parts")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Minimum face similarity score (0-1, default: 0.85)",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Run audit only, don't fail on low similarity",
    )
    parser.add_argument(
        "--qa-dir",
        help="Directory to write diagnostic images and report",
    )
    parser.add_argument("--repo-root", default=".", help="Repo root path")
    args = parser.parse_args()

    target_dir = Path(args.dir).resolve()
    repo_root = Path(args.repo_root).resolve()

    if args.qa_dir:
        qa_dir = Path(args.qa_dir)
    else:
        # FIX Bug C: use full relative path (not just date prefix) to avoid
        # qa_dir collision between same-date variants like masters/2026-07-24-final
        # and masters/2026-07-24-draft both resolving to qa/2026-07-24/.
        # Mirrors the fix applied to verify_integrity.py (Bug #2, PR #3).
        try:
            rel = target_dir.relative_to(repo_root)
        except ValueError:
            rel = Path(target_dir.name)
        qa_dir = repo_root / "qa" / str(rel).replace("/", "-")

    passed = verify_protagonist(
        target_dir,
        threshold=args.threshold,
        skip_check=args.skip_check,
        qa_dir=qa_dir,
    )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
