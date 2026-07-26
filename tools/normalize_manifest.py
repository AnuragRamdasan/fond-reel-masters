#!/usr/bin/env python3
"""
normalize_manifest.py – Manifest schema normalizer for fond-reel-masters.

Fixes (historical):
  Bug #3 (HIGH): Empty JSON {} misdetected as ads-bridge schema 1b due to
                 Python's all() returning True over an empty iterable (vacuous truth).

Fixes (2026-07-26):
  Bug D (HIGH): scan_all() iterated over p.iterdir() for ads-bridge/ and masters/
                without filtering to directories only. Loose files (README.md,
                .gitignore, etc.) were appended to dirs and passed to
                process_directory(), which called directory.rglob("*") on a file
                path → NotADirectoryError in Python 3.11+. Fix: add if sub.is_dir()
                guard in both loops.

  Bug H (HIGH): normalise_reel_manifest() only globbed master_part_*. Directories
                using bare part_0, part_1... naming (2026-07-09 era) produced an
                empty parts_detail list → manifest silently reports parts: 0 →
                downstream verify_integrity.py sees MISSING PARTS / UNTRACKED
                failures on every 2026-07-09 era directory.
                Fix: glob both master_part_* and part_*, deduplicate by filename,
                sort by name.

Usage:
    python tools/normalize_manifest.py              # dry-run (show what would change)
    python tools/normalize_manifest.py --write      # actually write manifests
    python tools/normalize_manifest.py --dir 2026-07-12
    python tools/normalize_manifest.py --all --write
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


SCHEMA_VERSION = 2
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# -------------------------------------------------------------------------------------
# Schema detection
# -------------------------------------------------------------------------------------

def detect_schema(directory: Path) -> Tuple[int, Optional[Dict]]:
    """
    Returns (schema_version, raw_data).
    schema_version: 0=missing, 1a=reel JSON, 1b=ads-bridge JSON, 1c=sha256 txt, 2=normalised
    """
    manifest_json = directory / "manifest.json"
    sha256_txt = directory / "master.sha256"
    sha256_txt2 = directory / "SHA256.txt"

    if manifest_json.exists():
        with open(manifest_json) as f:
            data = json.load(f)
        if data.get("schema") == SCHEMA_VERSION:
            return 2, data
        # Reel manifest: has 'date', 'arm', 'parts' as top-level scalars
        if "date" in data and "arm" in data and isinstance(data.get("parts"), int):
            return 1, data  # 1a
        # Ads-bridge manifest: values are dicts with sha256/bytes/parts
        # FIX Bug #3: guard against empty JSON {} – all() over empty iterable
        # returns True (vacuous truth), causing {} to be misdetected as schema 1b.
        if data and all(isinstance(v, dict) for v in data.values()):
            return 11, data  # 1b
        return 1, data

    for txt_path in [sha256_txt, sha256_txt2]:
        if txt_path.exists():
            with open(txt_path) as f:
                lines = [l.strip() for l in f if l.strip()]
            return 12, {"lines": lines, "source": str(txt_path)}  # 1c

    return 0, None  # missing


# -------------------------------------------------------------------------------------
# Normalisation functions
# -------------------------------------------------------------------------------------

def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    """Schema 1a → Schema 2."""
    # FIX Bug H: glob both master_part_* and part_* naming conventions,
    # deduplicate by filename, and sort by name so the list is deterministic.
    part_files = sorted(
        {p.name: p for p in list(directory.glob("master_part_*")) + list(directory.glob("part_*"))}.values(),
        key=lambda p: p.name,
    )
    parts_info = []
    for pf in part_files:
        parts_info.append({
            "file": pf.name,
            "bytes": pf.stat().st_size,
        })

    return {
        "schema": SCHEMA_VERSION,
        "date": raw.get("date", ""),
        "arm": raw.get("arm", ""),
        "parts": raw.get("parts", len(part_files)),
        "sha256": raw.get("sha256", ""),
        "size_bytes": raw.get("size", 0),
        "permalink": raw.get("permalink", ""),
        "uguu_video": raw.get("uguu_video", ""),
        "parts_detail": parts_info,
        "_migrated_from": "schema_1a",
    }


def normalise_ads_bridge_manifest(raw: Dict, directory: Path) -> Dict:
    """Schema 1b → Schema 2."""
    assets = {}
    for name, meta in raw.items():
        if isinstance(meta, dict):
            assets[name] = {
                "sha256": meta.get("sha256", ""),
                "bytes": meta.get("bytes", 0),
                "parts": meta.get("parts", 1),
            }

    return {
        "schema": SCHEMA_VERSION,
        "type": "ads-bridge",
        "assets": assets,
        "_migrated_from": "schema_1b",
    }

def normalise_sha256_txt(raw: Dict, directory: Path) -> Dict:
    """Schema 1c (sha256sum text) → Schema 2."""
    lines = raw.get("lines", [])
    source = raw.get("source", "master.sha256")
    assets = {}
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) == 2:
            digest, filename = parts
            filename = filename.lstrip("*").strip()
            assets[filename] = {"sha256": digest}

    return {
        "schema": SCHEMA_VERSION,
        "type": "reel-master",
        "assets": assets,
        "_migrated_from": f"schema_1c:{source}",
    }


def create_empty_manifest(directory: Path) -> Dict:
    """Schema 0 (missing) → Schema 2 stub."""
    date = ""
    m = DATE_RE.match(directory.name)
    if m:
        date = m.group(0)

    # FIX Bug #6: `not p.suffix in (...)` parses as `(not p.suffix) in (...)`
    # due to operator precedence, inverting the filter. Use `p.suffix not in (...)`.
    parts_detail = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.suffix not in (".jpg", ".png", ".json", ".txt", ".md"):
            parts_detail.append({
                "file": str(p.relative_to(directory)),
                "bytes": p.stat().st_size,
            })

    return {
        "schema": SCHEMA_VERSION,
        "date": date,
        "arm": "",
        "parts": len(parts_detail),
        "sha256": "",
        "size_bytes": sum(p["bytes"] for p in parts_detail),
        "permalink": "",
        "parts_detail": parts_detail,
        "_migrated_from": "schema_0_auto_generated",
        "_note": "AUTO-GENERATED: sha256 not verified. Run verify_integrity.py to compute.",
    }


# -------------------------------------------------------------------------------------
# Main scan
# -------------------------------------------------------------------------------------

def process_directory(directory: Path, write: bool = False) -> bool:
    """
    Normalise the manifest in a single directory.
    Returns True if any change was made/needed.
    """
    schema, raw = detect_schema(directory)

    if schema == 2:
        print(f"  ✅ {directory.name}: already schema v2, skipping")
        return False

    if schema == 0:
        normalised = create_empty_manifest(directory)
        action = "CREATE (schema 0 → 2)"
    elif schema == 1:
        normalised = normalise_reel_manifest(raw, directory)
        action = "MIGRATE (schema 1a → 2)"
    elif schema == 11:
        normalised = normalise_ads_bridge_manifest(raw, directory)
        action = "MIGRATE (schema 1b → 2)"
    elif schema == 12:
        normalised = normalise_sha256_txt(raw, directory)
        action = "MIGRATE (schema 1c → 2)"
    else:
        print(f"  ⚠️   {directory.name}: unknown schema {schema}, skipping")
        return False

    out_path = directory / "manifest.json"
    pencil = "\U0001f4dd"
    eye = "\U0001f441"
    print(f"  {pencil if write else eye} {directory.name}: {action}")

    if write:
        if out_path.exists():
            backup = directory / "manifest.json.bak"
            out_path.rename(backup)
            print(f"      Backed up old manifest to {backup.name}")
        with open(out_path, "w") as f:
            json.dump(normalised, f, indent=2)
        print(f"      Written: {out_path}")
    else:
        print(f"      Would write to: {out_path}")
        preview = json.dumps(normalised, indent=2)[:400]
        print(f"      Preview:\n{preview}\n      ...")

    return True


def scan_all(root: Path, write: bool = False) -> int:
    """Scan entire repo. Returns count of directories processed."""
    count = 0
    dirs = []

    for item in root.iterdir():
        if item.is_dir() and DATE_RE.match(item.name):
            dirs.append(item)

    # FIX Bug D: add `if sub.is_dir()` guard. Previously, p.iterdir() yielded
    # all entries including loose files (README.md, .gitignore, etc.). Passing a
    # file path to process_directory() caused detect_schema() to look for
    # "file/manifest.json" (never exists), then create_empty_manifest() to call
    # directory.rglob("*") on a file → NotADirectoryError in Python 3.11+.
    for parent in ["ads-bridge", "masters"]:
        p = root / parent
        if p.exists():
            for sub in p.iterdir():
                if sub.is_dir():  # FIX Bug D: only append actual directories
                    dirs.append(sub)

    for d in sorted(dirs):
        print(f"\n\U0001f4c1 {d.relative_to(root)}")
        changed = process_directory(d, write=write)
        if changed:
            count += 1

    return count


def main():
    parser = argparse.ArgumentParser(description="Normalise fond-reel-masters manifests to schema v2")
    parser.add_argument("--write", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--all", action="store_true", help="Scan entire repo")
    parser.add_argument("--dir", help="Process a single directory")
    parser.add_argument("--repo-root", default=".", help="Repo root path")
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()

    if not args.write:
        print("\U0001f481 DRY RUN – pass --write to apply changes\n")

    if args.dir:
        d = Path(args.dir).resolve()
        process_directory(d, write=args.write)
    elif args.all:
        n = scan_all(root, write=args.write)
        print(f"\n{'✅' if n else '–'} {n} director{'ies' if n != 1 else 'y'} {'updated' if args.write else 'would be updated'}.")
    else:
        n = scan_all(root, write=args.write)
        print(f"\n{'✅' if n else '–'} {n} director{'ies' if n != 1 else 'y'} {'updated' if args.write else 'would be updated'}.")


if __name__ == "__main__":
    main()
