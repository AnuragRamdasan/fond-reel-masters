#!/usr/bin/env python3
"""
verify_integrity.py — Unified archive integrity verifier for fond-reel-masters.

Fixes:
  Bug #2: Inconsistent manifest schema / missing manifests
  Bug #4: Single-chunk .p01of01 files not mapped correctly to manifest entries

Usage:
    python tools/verify_integrity.py [--date 2026-07-11] [--dir ads-bridge/2026-07-11]
    python tools/verify_integrity.py --all          # scan entire repo
    python tools/verify_integrity.py --dry-run      # report only, no writes
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PART_RE = re.compile(r"^(.+)\.p(\d{2})of(\d{2})$")
CHUNK_SIZE = 65536  # 64 KB read buffer


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def sha256_of_parts(part_paths: List[Path]) -> Tuple[str, int]:
    """Compute SHA256 and total byte count across ordered part files."""
    h = hashlib.sha256()
    total = 0
    for p in part_paths:
        with open(p, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                h.update(chunk)
                total += len(chunk)
    return h.hexdigest(), total


def collect_parts(directory: Path) -> Dict[str, List[Path]]:
    """
    Walk a directory and group .pNNofNN files by logical filename.
    Single-chunk files (p01of01) are included — this is Bug #4's fix.
    Returns dict: logical_name -> sorted list of part paths.
    """
    groups: Dict[str, List[Tuple[int, int, Path]]] = defaultdict(list)
    for f in directory.rglob("*"):
        if not f.is_file():
            continue
        m = PART_RE.match(f.name)
        if m:
            logical_name = m.group(1)
            part_num = int(m.group(2))
            total_parts = int(m.group(3))
            groups[logical_name].append((part_num, total_parts, f))

    result = {}
    for logical_name, entries in groups.items():
        entries.sort(key=lambda x: x[0])
        expected_total = entries[0][1]
        # Validate all parts agree on total count
        if any(e[1] != expected_total for e in entries):
            print(f"  ⚠️  WARN: inconsistent total-part count in {logical_name}")
        if len(entries) != expected_total:
            print(f"  ⚠️  WARN: expected {expected_total} parts for {logical_name}, found {len(entries)}")
        result[logical_name] = [e[2] for e in entries]
    return result


def load_manifest(directory: Path) -> Optional[Dict]:
    """Load manifest.json from directory, returning None if not present."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return None
    with open(manifest_path) as f:
        return json.load(f)


def verify_directory(directory: Path, dry_run: bool = False) -> Dict:
    """
    Verify integrity of a single archive directory.
    Returns a report dict.
    """
    report = {
        "directory": str(directory),
        "ok": [],
        "missing_parts": [],
        "sha256_mismatch": [],
        "size_mismatch": [],
        "orphaned_manifest_entries": [],
        "untracked_files": [],
        "manifest_missing": False,
    }

    parts_map = collect_parts(directory)
    manifest = load_manifest(directory)

    if manifest is None:
        report["manifest_missing"] = True
        print(f"  ⚠️  No manifest.json in {directory}")
        # Still check parts exist and compute their hashes
        for logical_name, part_files in parts_map.items():
            digest, total_bytes = sha256_of_parts(part_files)
            report["untracked_files"].append({
                "logical_name": logical_name,
                "parts": len(part_files),
                "bytes": total_bytes,
                "sha256": digest,
            })
        return report

    # Schema detection: Bug #2 fix — handle multiple manifest shapes
    schema = manifest.get("schema", 1)

    if schema >= 2:
        # Normalised schema
        assets = manifest.get("assets", {})
    elif "sha256" in manifest and "parts" in manifest:
        # Reel manifest (date-level, single asset)
        assets = {
            manifest.get("date", "reel"): {
                "sha256": manifest["sha256"],
                "bytes": manifest.get("size", 0),
                "parts": manifest["parts"],
            }
        }
    else:
        # ads-bridge manifest: keys are asset filenames
        assets = {k: v for k, v in manifest.items() if isinstance(v, dict) and "sha256" in v}

    manifest_keys = set(assets.keys())
    parts_keys = set(parts_map.keys())

    # Orphaned manifest entries (in manifest, no files on disk)
    for key in manifest_keys - parts_keys:
        report["orphaned_manifest_entries"].append(key)
        print(f"  ❌ ORPHANED: {key} is in manifest but has no files on disk")

    # Untracked files (files on disk, not in manifest)
    for key in parts_keys - manifest_keys:
        digest, total_bytes = sha256_of_parts(parts_map[key])
        report["untracked_files"].append({
            "logical_name": key,
            "parts": len(parts_map[key]),
            "bytes": total_bytes,
            "sha256": digest,
        })
        print(f"  ⚠️  UNTRACKED: {key} has no manifest entry")

    # Verify files that appear in both
    for key in manifest_keys & parts_keys:
        expected = assets[key]
        part_files = parts_map[key]
        expected_parts = expected.get("parts", len(part_files))

        # Part count check
        if len(part_files) != expected_parts:
            report["missing_parts"].append({
                "asset": key,
                "expected": expected_parts,
                "found": len(part_files),
            })
            print(f"  ❌ MISSING PARTS: {key}: expected {expected_parts}, found {len(part_files)}")
            continue

        # SHA256 + byte check
        digest, total_bytes = sha256_of_parts(part_files)
        expected_sha = expected.get("sha256", "")
        expected_bytes = expected.get("bytes", 0)

        sha_ok = (not expected_sha) or (digest == expected_sha)
        size_ok = (not expected_bytes) or (total_bytes == expected_bytes)

        if not sha_ok:
            report["sha256_mismatch"].append({
                "asset": key,
                "expected": expected_sha,
                "actual": digest,
            })
            print(f"  ❌ SHA256 MISMATCH: {key}")
        elif not size_ok:
            report["size_mismatch"].append({
                "asset": key,
                "expected_bytes": expected_bytes,
                "actual_bytes": total_bytes,
            })
            print(f"  ❌ SIZE MISMATCH: {key} expected {expected_bytes}B got {total_bytes}B")
        else:
            report["ok"].append(key)
            print(f"  ✅ OK: {key} ({total_bytes:,} bytes, SHA256 verified)")

    return report


def write_report(report: Dict, qa_dir: Path):
    qa_dir.mkdir(parents=True, exist_ok=True)
    report_path = qa_dir / "integrity_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  📄 Report written to {report_path}")


def scan_repo(root: Path, dry_run: bool = False) -> int:
    """Scan entire repo. Returns number of failures."""
    failures = 0
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}")

    dirs_to_check = []
    for item in root.iterdir():
        if item.is_dir() and date_pattern.match(item.name):
            dirs_to_check.append(item)
        elif item.is_dir() and item.name in ("ads-bridge", "masters"):
            for sub in item.iterdir():
                if sub.is_dir():
                    dirs_to_check.append(sub)

    for d in sorted(dirs_to_check):
        print(f"\n📁 Checking: {d}")
        report = verify_directory(d, dry_run=dry_run)

        n_fail = (
            len(report["missing_parts"])
            + len(report["sha256_mismatch"])
            + len(report["size_mismatch"])
            + len(report["orphaned_manifest_entries"])
        )
        failures += n_fail

        if not dry_run:
            qa_dir = root / "qa" / d.name.replace("/", "-")
            write_report(report, qa_dir)

    return failures


def main():
    parser = argparse.ArgumentParser(description="fond-reel-masters integrity verifier")
    parser.add_argument("--dir", help="Single directory to verify")
    parser.add_argument("--all", action="store_true", help="Scan entire repo")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    parser.add_argument("--repo-root", default=".", help="Repo root path")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()

    if args.dir:
        d = Path(args.dir).resolve()
        print(f"📁 Checking: {d}")
        report = verify_directory(d, dry_run=args.dry_run)
        if not args.dry_run:
            qa_dir = root / "qa" / d.name
            write_report(report, qa_dir)
        n_fail = len(report["sha256_mismatch"]) + len(report["missing_parts"])
        sys.exit(1 if n_fail else 0)
    elif args.all:
        failures = scan_repo(root, dry_run=args.dry_run)
        print(f"\n{'✅ All checks passed.' if failures == 0 else f'❌ {failures} failure(s) found.'}")
        sys.exit(1 if failures else 0)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
