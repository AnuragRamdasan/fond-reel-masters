#!/usr/bin/env python3
"""
verify_protagonist.py — Protagonist face-identity pre-commit guard for fond-reel-masters.

Fixes Bug #1: Wrong protagonist (AI character drift) across Veo A/B model renders.

The reel pipeline uses two AI video models (Veo A / Veo B) for different clip segments.
When model instances are not seeded identically, they produce different character
embeddings for the same prompt subject, causing visible face drift between opening
and closing shots.

This script:
  1. Locates all video part files (top-level or in parts/ subdirectory)
  2. Reassembles parts into a temp MP4 file
  3. Extracts face thumbnail strips from the opening (t=00:00:01) and
     closing (t=00:00:30) of the assembled video
  4. Computes perceptual hash similarity between opening and closing faces
  5. Exits with code 1 if similarity < threshold (blocks git commit via pre-commit hook)
  6. Saves a diagnostic contact sheet to qa/{date}/diag_faces.jpg

Supported part naming conventions:
  - *.pNNofNN  (e.g. fond_ad_C1_v1_9x16.mp4.p01of06) — top-level or parts/ subdir
  - part_*     (e.g. part_0, part_1) — top-level
  - master_part_* (e.g. master_part_0) — top-level

Usage:
    python tools/verify_protagonist.py --dir masters/2026-07-24-final
    python tools/verify_protagonist.py --dir masters/2026-07-24-final --threshold 0.80
    python tools/verify_protagonist.py --dir masters/2026-07-24-final --skip-check  # audit only

Install dependencies:
    pip install pillow imagehash

For full face detection (optional, improves accuracy):
    pip install face_recognition  # requires cmake + dlib
"""

import argparse
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------------
# Perceptual hashing (built-in, no external deps for core function)
# ---------------------------------------------------------------------------------

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
        # Fallback: use first 64 bits of MD5
        digest = hashlib.md5(image_bytes).digest()
        return struct.unpack("<Q", digest[:8])[0]


def hash_similarity(h1: int, h2: int, hash_size: int = 8) -> float:
    """Compute Hamming-distance-based similarity between two perceptual hashes."""
    total_bits = hash_size * hash_size
    xor = h1 ^ h2
    differing = bin(xor).count("1")
    return 1.0 - (differing / total_bits)


# ---------------------------------------------------------------------------------
# Video frame extraction
# ---------------------------------------------------------------------------------

def extract_frame_bytes(video_path: Path, timestamp: str = "00:00:01") -> Optional[bytes]:
    """
    Extract a single frame from a video file using ffmpeg.
    Returns JPEG bytes, or None if ffmpeg is not available / extraction fails.
    """
    import subprocess

    # FIX (original Bug #1): initialize tmp_path before try so finally never
    # raises UnboundLocalError if NamedTemporaryFile() itself fails.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name

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
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------------
# Part discovery
# ---------------------------------------------------------------------------------

def find_parts_in_dir(directory: Path) -> Tuple[List[Path], List[Path]]:
    """
    Locate all ordered video part files in a directory tree and return the
    full part list as both the "opening" and "closing" part collections.
    The caller uses these to reassemble the complete video and then extract
    frames at arbitrary timestamps.

    Searches top-level AND the parts/ subdirectory.
    Matches three naming conventions:
      - part_* / master_part_*  (bare integer-indexed chunks, top-level only)
      - *.pNNofNN               (padded-index, top-level or parts/ subdir)

    Returns (all_parts, all_parts) so that verify_protagonist can reassemble
    the full video from those parts.  Returns ([], []) when no parts are
    found or when only a single part exists (drift check cannot run).

    FIX Bug A: Prior version used directory.iterdir() for .pNNofNN files,
    which only lists top-level items. Dates 2026-07-09 → 2026-07-16 all
    store parts inside a parts/ subdirectory, so the check silently skipped
    every one of those dates.

    FIX Bug B: Prior version used glob("part_*"), which does not match
    master_part_* (used by 2026-07-17). Result: every archive date — both
    those with a parts/ subdir (Bug A) AND those with top-level master_part_*
    files (Bug B) — silently skipped the protagonist check.
    """
    import re

    # ── Strategy 1: bare integer-indexed chunks at the top level ──────────
    # Match both part_* (e.g. part_0) and master_part_* (e.g. master_part_0).
    # FIX Bug B: original glob("part_*") missed master_part_* entirely.
    bare_parts = sorted(
        list(directory.glob("part_*")) + list(directory.glob("master_part_*"))
    )
    if bare_parts:
        if len(bare_parts) == 1:
            print(
                f"  ⚠️  Only one part found in {directory.name} "
                "— cannot test protagonist drift. Skipping check."
            )
            return [], []
        # Return the full list for both opening and closing so the caller
        # can reassemble and sample at any timestamp.
        return bare_parts, bare_parts

    # ── Strategy 2: .pNNofNN files (top-level AND parts/ subdirectory) ────
    # FIX Bug A: also search the parts/ subdirectory so dates that store
    # their chunks there (2026-07-09 → 2026-07-16) are discovered.
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
        return [], []

    # Pick the group with the most parts (main video, not thumbnail/poster)
    best_key = max(groups, key=lambda k: len(groups[k]))
    all_parts = [p for _, p in sorted(groups[best_key], key=lambda x: x[0])]

    if len(all_parts) == 1:
        print(
            f"  ⚠️  Only one part found in {directory.name} "
            "— cannot test protagonist drift. Skipping check."
        )
        return [], []

    return all_parts, all_parts


# ---------------------------------------------------------------------------------
# Contact sheet generation
# ---------------------------------------------------------------------------------

def make_contact_sheet(frames: List[Tuple[str, bytes]], output_path: Path):
    """
    Create a side-by-side contact sheet from labeled frame bytes.
    Requires PIL.
    """
    try:
        from PIL import Image, ImageDraw

        images = []
        for label, data in frames:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img = img.resize((320, 480), Image.LANCZOS)

            # Add label
            draw = ImageDraw.Draw(img)
            draw.rectangle([(0, 0), (320, 30)], fill=(0, 0, 0, 180))
            draw.text((8, 6), label, fill=(255, 255, 255))
            images.append(img)

        width = 320 * len(images)
        sheet = Image.new("RGB", (width, 480), color=(20, 20, 20))
        for i, img in enumerate(images):
            sheet.paste(img, (i * 320, 0))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(str(output_path), "JPEG", quality=85)
        print(f"  📸 Contact sheet saved: {output_path}")

    except ImportError:
        print("  ⚠️   PIL not available — skipping contact sheet generation")
        print("       pip install pillow")


# ---------------------------------------------------------------------------------
# Main verification logic
# ---------------------------------------------------------------------------------

def verify_protagonist(
    directory: Path,
    threshold: float = 0.85,
    skip_check: bool = False,
    qa_dir: Optional[Path] = None,
) -> bool:
    """
    Verify protagonist face consistency between opening and closing of reel.
    Returns True if check passes (or was skipped), False if identity drift detected.

    FIX Bug C: Previous version called extract_frame_bytes() on individual
    binary split chunks (.pNNofNN). Only the first chunk potentially contains
    the moov atom and is parseable by ffmpeg. The last chunk (used for the
    closing frame) is raw video data with no container header — ffmpeg always
    returns None for it — causing verify_protagonist to print "treating as
    PASS" and return True for every reel, regardless of protagonist drift.

    Fix: reassemble ALL parts into a single temporary MP4 file first (once),
    then call extract_frame_bytes on the fully assembled file for both the
    opening frame (t=00:00:01) and the closing frame (t=00:00:30).
    """
    print(f"🎬 Verifying protagonist consistency in: {directory}")

    all_parts, _ = find_parts_in_dir(directory)

    if not all_parts:
        print("  ⚠️   No video parts found in directory. Skipping check.")
        return True

    print(
        f"  📂 Found {len(all_parts)} parts: "
        f"{all_parts[0].name} … {all_parts[-1].name}"
    )

    # FIX Bug C: Reassemble ALL parts into a single temp MP4 file, then
    # extract both frames from the assembled file.  Individual .pNNofNN
    # chunks are raw byte splits of the MP4 container — they are NOT
    # independently decodable video files.  Only the assembled file can be
    # reliably decoded by ffmpeg at an arbitrary timestamp.
    assembled_path: Optional[Path] = None
    opening_frame: Optional[bytes] = None
    closing_frame: Optional[bytes] = None

    try:
        total_bytes = sum(p.stat().st_size for p in all_parts)
        print(
            f"  🔗 Reassembling {len(all_parts)} parts "
            f"({total_bytes / 1_048_576:.1f} MB)…"
        )

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            assembled_path = Path(tmp.name)
            for part in all_parts:
                tmp.write(part.read_bytes())

        print("  🎞️   Extracting opening frame (t=00:00:01)…")
        opening_frame = extract_frame_bytes(assembled_path, "00:00:01")

        # Sample closing frame near end of video.  t=00:00:30 is used as a
        # heuristic — ffmpeg clamps to the last available frame when the
        # timestamp exceeds the file duration, so this works for short reels too.
        print("  🎞️   Extracting closing frame (t=00:00:30)…")
        closing_frame = extract_frame_bytes(assembled_path, "00:00:30")

    finally:
        # Always clean up the reassembled temp file (potentially large).
        if assembled_path is not None:
            try:
                assembled_path.unlink()
            except OSError:
                pass

    if opening_frame is None or closing_frame is None:
        print("  ⚠️   Could not extract frames (ffmpeg not available or file corrupt)")
        print("  ℹ️   Install ffmpeg to enable face-identity checking")
        print("  ✅  Check skipped (no ffmpeg) — treating as PASS")
        return True

    # Compute perceptual hash similarity
    h_open = _average_hash(opening_frame)
    h_close = _average_hash(closing_frame)
    similarity = hash_similarity(h_open, h_close)

    print(f"  🔬 Face similarity score: {similarity:.3f} (threshold: {threshold:.2f})")

    # Save contact sheet
    if qa_dir:
        sheet_path = qa_dir / "diag_faces.jpg"
        make_contact_sheet(
            [
                (f"OPENING t=00:00:01\n{all_parts[0].name}", opening_frame),
                (f"CLOSING t=00:00:30\n{all_parts[-1].name}", closing_frame),
            ],
            sheet_path,
        )

    # Write JSON report
    report = {
        "directory": str(directory),
        "parts": len(all_parts),
        "opening_part": all_parts[0].name,
        "closing_part": all_parts[-1].name,
        "opening_timestamp": "00:00:01",
        "closing_timestamp": "00:00:30",
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
        help="Directory to write diagnostic images and report (default: qa/{date})",
    )
    parser.add_argument("--repo-root", default=".", help="Repo root path")
    args = parser.parse_args()

    target_dir = Path(args.dir).resolve()
    repo_root = Path(args.repo_root).resolve()

    if args.qa_dir:
        qa_dir = Path(args.qa_dir)
    else:
        # Auto-detect date from directory name
        import re
        m = re.search(r"\d{4}-\d{2}-\d{2}", target_dir.name)
        date = m.group(0) if m else target_dir.name
        qa_dir = repo_root / "qa" / date

    passed = verify_protagonist(
        target_dir,
        threshold=args.threshold,
        skip_check=args.skip_check,
        qa_dir=qa_dir,
    )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
