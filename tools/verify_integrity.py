#!/usr/bin/env python3
"""
verify_integrity.py – Unified archive integrity verifier for fond-reel-masters.

Fixes:
  Bug #1 (CRITICAL): manifest_missing not counted in exit code → silent CI pass
  Bug #2 (HIGH): QA report collision when same-named dirs exist under different parents
  Bug #4: Single-chunk .p01of01 files not mapped correctly to manifest entries
  Bug I (LOW): Dead sha256_of_bytes() function removed – it was never called and
               loading multi-GB video files into memory would OOM the process. All
               hashing uses the streaming sha256_of_parts() below.
  Bug J (HIGH): collect_parts() silently misses bare part_* / master_part_* files
               (2026-07-09-era directories). Now also globs bare parts under a
               "__bare__" sentinel key and reconciles in verify_directory().
  Bug K (MEDIUM): Schema-2 reel manifests skipped SHA256 verification silently.
               assets = manifest.get("assets", {}) returns {} for reel manifests
               that have no "assets" key. Now falls through to the sha256/parts
               branch for single-asset reel manifests.
  Bug L (LOW): Lexicographic sort orders part_10 before part_2 for >=10 parts.
               Fixed with _natural_sort_key() helper using re.split on digit runs.
  Bug O (HIGH): scan_repo() hard-coded only ("ads-bridge", "masters") as parent
               directories to descend into. audition/, covers/, drafts/, and
               screen-inserts/ were silently skipped, leaving their archive content
               unprotected by integrity checks. Fixed with dynamic parent discovery:
               any top-level directory that is not date-named AND not in a known-skip
               set is treated as a parent of archive subdirectories.

Usage:
    python tools/verify_integrity.py [--date 2026-07-11] [--dir ads-bridge/2026-07-11]
    python tools/verify_integrity.py --all            # scan entire repo
    python tools/verify_integrity.py --dry-run        # report only, no writes
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

# FIX Bug O: directories that should never be treated as archive parents.
# These are repo-infrastructure dirs, not content dirs.
_SCAN_SKIP_DIRS = frozenset({
    ".github", ".git", "tools", "docs", "qa",
})


# sha256_of_bytes() removed (Bug I fix): dead function never called; all hashing uses
# the streaming sha256_of_parts() below to avoid loading multi-GB files into memory.


def _natural_sort_key(s: str) -> list:
    """
    FIX Bug L: Natural sort key so part_10 sorts after part_2, not before.
    Splits the string on digit runs and converts digit segments to int.
    """
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", s)]


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
    Single-chunk files (p01of01) are included.
    Returns dict: logical_name -> sorted list of part paths.

    FIX Bug J: Also globs bare part_* / master_part_* files (2026-07-09-era
    directories). These are collected under the sentinel key "__bare__" so
    verify_directory() can reconcile them against the manifest.

    FIX Bug L: Bare parts are sorted with _natural_sort_key to handle >=10 parts.
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
        if any(e[1] != expected_total for e in entries):
            print(f"  ⚠️   WARN: inconsistent total-part count in {logical_name}")
        if len(entries) != expected_total:
            print(f"  ⚠️   WARN: expected {expected_total} parts for {logical_name}, found {len(entries)}")
        result[logical_name] = [e[2] for e in entries]

    # FIX Bug J: collect bare part_* and master_part_* files (files only, no subdirs)
    # Use a name-keyed dict merge to deduplicate across both globs.
    bare_files = list(
        {p.name: p for p in list(directory.glob("part_*")) + list(directory.glob("master_part_*"))
         if p.is_file()}.values()
    )
    if bare_files:
        # FIX Bug L: sort bare parts with natural sort key
        result["__bare__"] = sorted(bare_files, key=lambda p: _natural_sort_key(p.name))

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
        "unverified": [],
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
        print(f"  ⚠️   No manifest.json in {directory}")
        for logical_name, part_files in parts_map.items():
            if logical_name == "__bare__":
                continue
            digest, total_bytes = sha256_of_parts(part_files)
            report["untracked_files"].append({
                "logical_name": logical_name,
                "parts": len(part_files),
                "bytes": total_bytes,
                "sha256": digest,
            })
        # Also report bare parts if no manifest
        if "__bare__" in parts_map:
            bare = parts_map["__bare__"]
            digest, total_bytes = sha256_of_parts(bare)
            report["untracked_files"].append({
                "logical_name": "__bare__",
                "parts": len(bare),
                "bytes": total_bytes,
                "sha256": digest,
            })
        return report

    schema = manifest.get("schema", 1)

    if schema >= 2:
        assets = manifest.get("assets", {})
        # FIX Bug K: schema-2 reel manifests have no "assets" key but DO have a
        # top-level "sha256". The old code returned {} and skipped verification.
        # Now we fall through to build a single-asset dict just like schema-1a.
        if not assets and "sha256" in manifest:
            assets = {
                manifest.get("date", "reel"): {
                    "sha256": manifest["sha256"],
                    "bytes": manifest.get("size_bytes", manifest.get("size", 0)),
                    "parts": manifest.get("parts", 1),
                }
            }
    elif "sha256" in manifest and "parts" in manifest:
        assets = {
            manifest.get("date", "reel"): {
                "sha256": manifest["sha256"],
                "bytes": manifest.get("size", 0),
                "parts": manifest["parts"],
            }
        }
    else:
        assets = {k: v for k, v in manifest.items() if isinstance(v, dict) and "sha256" in v}

    # FIX Bug J: reconcile __bare__ sentinel against manifest keys.
    # For the common single-asset reel case: if exactly one manifest key has no
    # corresponding pNNofNN group, map __bare__ to that manifest key.
    if "__bare__" in parts_map:
        bare_files = parts_map.pop("__bare__")
        manifest_keys_set = set(assets.keys())
        pNNofNN_keys_set = set(parts_map.keys())
        unmatched_manifest = manifest_keys_set - pNNofNN_keys_set
        if len(unmatched_manifest) == 1:
            # Unambiguous: map bare files to the one unmatched manifest key
            sole_key = next(iter(unmatched_manifest))
            parts_map[sole_key] = bare_files
        else:
            # Ambiguous or all manifest keys already matched: treat as untracked
            digest, total_bytes = sha256_of_parts(bare_files)
            report["untracked_files"].append({
                "logical_name": "__bare__",
                "parts": len(bare_files),
                "bytes": total_bytes,
                "sha256": digest,
            })
            print(f"  ⚠️   UNTRACKED (bare): could not unambiguously map bare parts to manifest key")

    manifest_keys = set(assets.keys())
    parts_keys = set(parts_map.keys())

    for key in manifest_keys - parts_keys:
        report["orphaned_manifest_entries"].append(key)
        print(f"  ❌ ORPHANED: {key} is in manifest but has no files on disk")

    for key in parts_keys - manifest_keys:
        digest, total_bytes = sha256_of_parts(parts_map[key])
        report["untracked_files"].append({
            "logical_name": key,
            "parts": len(parts_map[key]),
            "bytes": total_bytes,
            "sha256": digest,
        })
        print(f"  ⚠️   UNTRACKED: {key} has no manifest entry")

    for key in manifest_keys & parts_keys:
        expected = assets[key]
        part_files = parts_map[key]
        expected_parts = expected.get("parts", len(part_files))

        if len(part_files) != expected_parts:
            report["missing_parts"].append({
                "asset": key,
                "expected": expected_parts,
                "found": len(part_files),
            })
            print(f"  ❌ MISSING PARTS: {key}: expected {expected_parts}, found {len(part_files)}")
            continue

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
            actually_verified = bool(expected_sha) or bool(expected_bytes)
            if actually_verified:
                report["ok"].append(key)
                print(f"  ✅ OK: {key} ({total_bytes:,} bytes, SHA256 verified)")
            else:
                report["unverified"].append(key)
                print(f"  ⚠️  UNVERIFIED: {key} – no SHA256 or size in manifest")

    return report


def write_report(report: Dict, qa_dir: Path):
    qa_dir.mkdir(parents=True, exist_ok=True)
    report_path = qa_dir / "integrity_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  \U0001f4c4 Report written to {report_path}")


def scan_repo(root: Path, dry_run: bool = False) -> int:
    """Scan entire repo. Returns number of failures.

    FIX Bug O: Previously only descended into 'ads-bridge' and 'masters'.
    Now dynamically discovers all top-level directories that are not date-named
    and not in _SCAN_SKIP_DIRS, treating them as parents of archive subdirectories.
    This ensures audition/, covers/, drafts/, screen-inserts/ (and any future
    content parent directories) are included in integrity checks.
    """
    failures = 0
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}")

    dirs_to_check = []
    for item in root.iterdir():
        if not item.is_dir():
            continue
        if item.name.startswith("."):
            continue
        if date_pattern.match(item.name):
            # Top-level date directory: check directly
            dirs_to_check.append(item)
        elif item.name not in _SCAN_SKIP_DIRS:
            # FIX Bug O: treat any non-skipped, non-date top-level dir as an
            # archive parent and descend into its subdirectories.
            for sub in item.iterdir():
                if sub.is_dir():
                    dirs_to_check.append(sub)

    for d in sorted(dirs_to_check):
        print(f"\n\U0001f4c1 Checking: {d}")
        report = verify_directory(d, dry_run=dry_run)

        # FIX Bug #1: include manifest_missing in failure count
        n_fail = (
            len(report["missing_parts"])
            + len(report["sha256_mismatch"])
            + len(report["size_mismatch"])
            + len(report["orphaned_manifest_entries"])
            + (1 if report.get("manifest_missing") else 0)
        )
        failures += n_fail

        if not dry_run:
            # FIX Bug #2: use full relative path to avoid collision between
            # same-named dirs under different parents (e.g. ads-bridge/2026-07-11
            # and masters/2026-07-11 both have d.name == '2026-07-11').
            qa_dir = root / "qa" / str(d.relative_to(root)).replace("/", "-")
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
        print(f"\U0001f4c1 Checking: {d}")
        report = verify_directory(d, dry_run=args.dry_run)
        if not args.dry_run:
            # FIX Bug #2: unique qa dir per relative path, not just d.name
            qa_dir = root / "qa" / str(d.relative_to(root)).replace("/", "-")
            write_report(report, qa_dir)
        # FIX Bug #1: count ALL failure categories including manifest_missing
        n_fail = (
            len(report["sha256_mismatch"])
            + len(report["missing_parts"])
            + len(report["size_mismatch"])
            + len(report["orphaned_manifest_entries"])
            + (1 if report.get("manifest_missing") else 0)
        )
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
