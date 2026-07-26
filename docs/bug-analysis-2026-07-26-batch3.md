# Bug Analysis — fond-reel-masters Batch 3 (2026-07-26)

**Repo:** `AnuragRamdasan/fond-reel-masters`  
**Analysis date:** 2026-07-26  
**Analyst:** Parallel Loop AI  
**Files audited:** `tools/normalize_manifest.py`, `tools/verify_integrity.py`, `tools/verify_protagonist.py`  
**Prior bug count:** A–O (15 bugs across 4 batches, all fixed)  
**New bugs found:** 3 (P, Q, R) — all in `normalize_manifest.py`

---

## Summary Table

| ID | Severity | File | Function | Root Cause | Status |
|----|----------|------|----------|------------|--------|
| P  | **HIGH** | `normalize_manifest.py` | `scan_all()` | Hard-coded only `ads-bridge` + `masters` as parent dirs — mirrors Bug O (fixed in `verify_integrity.py`) but left unfixed in `normalize_manifest.py` | **Fixed in this batch** |
| Q  | **MEDIUM** | `normalize_manifest.py` | `create_empty_manifest()` | `rglob("*")` includes `manifest.json.bak` backup files, inflating `parts` count and `size_bytes` in auto-generated stubs | **Fixed in this batch** |
| R  | **LOW** | `normalize_manifest.py` | `scan_all()` | `sorted(dirs)` sorts `Path` objects by absolute path string, varying by machine/CI env — non-deterministic output order | **Fixed in this batch** |

---

## Detailed Analysis

---

### Bug P — HIGH — `scan_all()` parity gap with verify_integrity.py

**File:** `tools/normalize_manifest.py` · **Function:** `scan_all()`

#### Root Cause

`scan_all()` only descends into the hard-coded parent directories `ads-bridge` and `masters`:

```python
for parent in ["ads-bridge", "masters"]:
    p = root / parent
    if p.exists():
        for sub in p.iterdir():
            if sub.is_dir():
                dirs.append(sub)
```

The repo also contains `audition/`, `covers/`, `drafts/`, and `screen-inserts/` as top-level archive parent directories. Manifests in those trees are **never normalised** by `normalize_manifest.py --all`.

This is the exact same pattern as **Bug O** (discovered in `verify_integrity.py`), which was already fixed in `verify_integrity.py`'s `scan_repo()` by switching to dynamic discovery using `_SCAN_SKIP_DIRS`. The fix was not ported to `normalize_manifest.py`.

#### Impact

- Running `python tools/normalize_manifest.py --all --write` silently leaves manifests in `audition/`, `covers/`, `drafts/`, `screen-inserts/` at whatever schema they currently have.
- `verify_integrity.py --all` (which was fixed) subsequently discovers those directories and reports failures that `normalize_manifest.py` should have resolved.
- The CI pipeline produces false INTEGRITY FAIL results for any content added to `covers/`, `audition/`, etc. since these manifests are never migrated to schema v2.

#### Fix

Port the dynamic discovery logic from `verify_integrity.py`'s `scan_repo()` using `_SCAN_SKIP_DIRS`.

---

### Bug Q — MEDIUM — `create_empty_manifest()` counts `.bak` backup files as archive parts

**File:** `tools/normalize_manifest.py` · **Function:** `create_empty_manifest()`

#### Root Cause

When a manifest backup (`manifest.json.bak`) exists, `create_empty_manifest()` uses `rglob("*")` and filters by suffix:

```python
if p.is_file() and p.suffix not in (".jpg", ".png", ".json", ".txt", ".md"):
```

The suffix `.bak` is **not in the exclusion list**, so backup files are counted as video parts, inflating `"parts"` and `"size_bytes"` in the auto-generated stub manifest.

#### Impact

- Any directory re-processed by `normalize_manifest.py --write` accumulates `.bak` files that pollute the new manifest's `parts_detail`.
- `verify_integrity.py` sees `"parts": N+1` in the manifest but only finds `N` real video files → reports MISSING PARTS failure.
- Each additional `--write` run worsens the mismatch.

#### Fix

Add `.bak` to the suffix exclusion tuple.

---

### Bug R — LOW — Non-deterministic directory sort in `scan_all()`

**File:** `tools/normalize_manifest.py` · **Function:** `scan_all()`

#### Root Cause

```python
for d in sorted(dirs):
```

`sorted()` on `Path` objects compares by absolute path string. Output ordering varies between developer machines (`/Users/anurag/...`) and CI (`/home/runner/work/...`), making console output non-reproducible.

#### Fix

Sort by path relative to `root`:

```python
for d in sorted(dirs, key=lambda p: p.relative_to(root)):
```

---

## Files Changed

| File | Change |
|------|--------|
| `tools/normalize_manifest.py` | Fix P: dynamic `scan_all()` discovery; Fix Q: add `.bak` to exclusion list; Fix R: sort by `relative_to(root)` |
| `docs/bug-analysis-2026-07-26-batch3.md` | This analysis document |
