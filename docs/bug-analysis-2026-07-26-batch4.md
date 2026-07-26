# Bug Analysis — fond-reel-masters Batch 4 (2026-07-26)

Three new bugs (S, T, U) identified by automated code review after all A–R fixes landed.
All are in the Python tooling layer (`tools/`).

---

## Bug S — HIGH · `normalize_manifest.py` · Natural sort missing in `normalise_reel_manifest()`

### Location
`tools/normalize_manifest.py` → `normalise_reel_manifest()`, line:
```python
part_files = sorted(
    {p.name: p for p in list(directory.glob("master_part_*")) + list(directory.glob("part_*"))}.values(),
    key=lambda p: p.name,          # ← BUG S: alphabetic sort
)
```

### Root Cause
`sorted(..., key=lambda p: p.name)` performs a **plain string / lexicographic sort**.
For archives with ≥ 10 parts the ordering becomes wrong:

```
part_1, part_10, part_11, part_2, part_3, ...   # wrong
part_1, part_2,  part_3,  ...,   part_10, ...   # correct
```

The same defect was already identified and fixed in two other locations:
- `verify_integrity.py` → `collect_parts()` — Bug L fix
- `verify_protagonist.py` → `find_parts_in_dir()` — Bug L fix

However `normalise_reel_manifest()` in `normalize_manifest.py` was not updated during
the Bug H fix (batch 1), which only added the dual-glob logic without fixing the sort.

### Impact
The `parts_detail` array written into `manifest.json` lists parts in the wrong order
for any reel with ≥ 10 parts. Any consumer that reads `parts_detail` in sequence
(e.g. a downstream render or upload script) will process parts in the wrong order,
producing a corrupted output with clip 10 appearing before clip 2.

### Fix
Replace `key=lambda p: p.name` with `key=lambda p: _natural_sort_key(p.name)`.
A module-level `_natural_sort_key` helper must also be added (mirrors the helper
already present in `verify_integrity.py` and `verify_protagonist.py`).

---

## Bug T — MEDIUM · `verify_protagonist.py` · OOM in part reassembly

### Location
`tools/verify_protagonist.py` → `verify_protagonist()`, assembly loop:
```python
with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
    assembled_path = Path(tmp.name)
    for part in all_parts:
        tmp.write(part.read_bytes())   # ← BUG T: loads each part fully into RAM
```

### Root Cause
`part.read_bytes()` reads the **entire file into a Python bytes object** before
writing it to the temp file. A typical fond-reel-masters reel has 3 × Veo clips
each ~250–600 MB, totalling 750 MB – 1.8 GB. Loading each part as a full
`bytes` object peaks RAM usage at `2 × part_size` (read buffer + write buffer).
On constrained CI runners or memory-limited containers this causes an OOM kill,
crashing the entire pre-commit hook silently with no diagnostic output.

The same pattern was already flagged for `sha256_of_bytes()` (Bug I, batch 2),
which was removed. The assembly loop in `verify_protagonist()` has the same
defect but was missed because it is conceptually "write, not hash".

### Fix
Replace `part.read_bytes()` with a chunked copy loop using an 8 MB read buffer.
This keeps peak RAM usage at `8 MB` regardless of part size:
```python
COPY_CHUNK = 8 * 1024 * 1024  # 8 MB
for part in all_parts:
    with open(part, "rb") as src:
        while True:
            chunk = src.read(COPY_CHUNK)
            if not chunk:
                break
            tmp.write(chunk)
```

---

## Bug U — LOW · `verify_integrity.py` · Non-deterministic sort in `scan_repo()`

### Location
`tools/verify_integrity.py` → `scan_repo()`:
```python
for d in sorted(dirs_to_check):     # ← BUG U: sorts Path objects by absolute path
```

### Root Cause
`sorted(dirs_to_check)` compares `Path` objects, which fall back to comparing
their **absolute string representations**. The absolute path includes the
machine-specific checkout root (e.g. `/home/runner/work/fond-reel-masters/…`
vs `/Users/anurag/code/fond-reel-masters/…`), so console output order is
non-deterministic across developer machines and CI environments.

The exact same bug was already identified and fixed in `normalize_manifest.py`'s
`scan_all()` as Bug R (batch 3):
```python
# normalize_manifest.py — Bug R fix already applied:
for d in sorted(dirs, key=lambda p: p.relative_to(root)):
```

But the corresponding `scan_repo()` function in `verify_integrity.py` was not
updated at the same time.

### Impact
Low — no data corruption. But it makes CI log comparison and regression diffing
unreliable, and can make flaky test failures harder to diagnose.

### Fix
Replace `sorted(dirs_to_check)` with `sorted(dirs_to_check, key=lambda p: p.relative_to(root))`.

---

## Summary Table

| Bug | Severity | File | Function | Issue |
|-----|----------|------|----------|-------|
| S | HIGH | `normalize_manifest.py` | `normalise_reel_manifest()` | Alphabetic sort corrupts `parts_detail` order for ≥10 parts |
| T | MEDIUM | `verify_protagonist.py` | `verify_protagonist()` | `read_bytes()` OOM on large multi-part reels |
| U | LOW | `verify_integrity.py` | `scan_repo()` | Non-deterministic sort by absolute path |

All three fixes are minimal, targeted, and do not change any public API or manifest
schema. Regression risk is low.
