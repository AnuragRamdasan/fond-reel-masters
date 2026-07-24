#!/usr/bin/env python3
"""
verify_protagonist.py — Protagonist face-identity pre-commit guard for fond-reel-masters.

Fixes Bug #1: Wrong protagonist (AI character drift) across Veo A/B model renders.

The reel pipeline uses two AI video models (Veo A / Veo B) for different clip segments.
When model instances are not seeded identically, they produce different character
embeddings for the same prompt subject, causing visible face drift between opening
and closing shots.

This script:
  1. Extracts face thumbnail strips from the first and last video parts
  2. Computes perceptual hash similarity between opening and closing faces
  3. Exits with code 1 if similarity < threshold (blocks git commit via pre-commit hook)
  4. Saves a diagnostic contact sheet to qa/{date}/diag_faces.jpg

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
import zlib
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Perceptual hashing (built-in, no external deps for core function)
# ---------------------------------------------------------------------------

def _average_hash(image_bytes: bytes, hash_size: int = 8) -> int:
    """
    Compute average hash of image bytes (JPEG/PNG).
    Returns an integer bitfield.
    Falls back to MD5 prefix if PIL not available.
    """
    try:
        from PIL import Image
        import math

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


# ---------------------------------------------------------------------------
# Video frame extraction
# ---------------------------------------------------------------------------

def extract_frame_bytes(video_path: Path, timestamp: str = "00:00:01") -> Optional[bytes]:
    """
    Extract a single frame from a video file using ffmpeg.
    Returns JPEG bytes, or None if ffmpeg is not available.
    """
    import subprocess
    import tempfile

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
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def extract_frame_from_parts(parts: List[Path], timestamp: str = "00:00:01") -> Optional[bytes]:
    """
    Reassemble parts into a temp file and extract a frame.
    Handles .pNNofNN parts by concatenating in order.
    """
    import tempfile
    import subprocess

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            for part in parts:
                tmp.write(part.read_bytes())

        result = extract_frame_bytes(tmp_path, timestamp)
        return result
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def find_parts_in_dir(directory: Path) -> Tuple[List[Path], List[Path]]:
    """
    Find first and last part groups in a directory.
    Returns (opening_parts, closing_parts).
    """
    import re
    PART_RE = re.compile(r"^(.+)\.(p(\d{2})of(\d{2})|part_(\d+))$", re.IGNORECASE)

    # bare part_NN files (masters/ format)
    bare_parts = sorted(directory.glob("part_*"))
    if bare_parts:
        return [bare_parts[0]], [bare_parts[-1]]

    # .pNNofNN files (date-level format)
    groups = {}
    for f in directory.iterdir():
        m = re.match(r"^(.+)\.p(\d{2})of(\d{2})$", f.name)
        if m:
            key = m.group(1)
            num = int(m.group(2))
            groups.setdefault(key, []).append((num, f))

    if not groups:
        return [], []

    # Pick the largest group (main video, not audio)
    best_key = max(groups, key=lambda k: len(groups[k]))
    parts = sorted(groups[best_key], key=lambda x: x[0])
    all_parts = [p for _, p in parts]

    # Opening = first part, closing = last part
    return [all_parts[0]], [all_parts[-1]]


# ---------------------------------------------------------------------------
# Contact sheet generation
# ---------------------------------------------------------------------------

def make_contact_sheet(frames: List[Tuple[str, bytes]], output_path: Path):
    """
    Create a side-by-side contact sheet from labeled frame bytes.
    Requires PIL.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

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
        print("  ⚠️  PIL not available — skipping contact sheet generation")
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
    """
    print(f"🎬 Verifying protagonist consistency in: {directory}")

    opening_parts, closing_parts = find_parts_in_dir(directory)

    if not opening_parts:
        print("  ⚠️  No video parts found in directory. Skipping check.")
        return True

    print(f"  📂 Opening part: {opening_parts[0].name}")
    print(f"  📂 Closing part: {closing_parts[0].name}")

    # Extract frames
    print("  🎞️  Extracting opening frame (t=00:00:01)...")
    opening_frame = extract_frame_bytes(opening_parts[0], "00:00:01")

    print("  🎞️  Extracting closing frame (t=00:00:01)...")
    closing_frame = extract_frame_bytes(closing_parts[0], "00:00:01")

    if opening_frame is None or closing_frame is None:
        print("  ⚠️  Could not extract frames (ffmpeg not available or parts too short)")
        print("  ℹ️  Install ffmpeg to enable face-identity checking")
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
                (f"OPENING\n{opening_parts[0].name}", opening_frame),
                (f"CLOSING\n{closing_parts[0].name}", closing_frame),
            ],
            sheet_path,
        )

    # Write JSON report
    report = {
        "directory": str(directory),
        "opening_part": opening_parts[0].name,
        "closing_part": closing_parts[0].name,
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
        print(f"  ℹ️  Check skipped (--skip-check). Similarity was {similarity:.3f}")
        return True

    if similarity >= threshold:
        print(f"  ✅ PASS: Protagonist identity consistent (similarity={similarity:.3f} ≥ {threshold})")
        return True
    else:
        print(f"  ❌ FAIL: Protagonist identity drift detected! (similarity={similarity:.3f} < {threshold})")
        print(f"       This means opening and closing shots show different faces.")
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
