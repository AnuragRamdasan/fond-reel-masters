# Bug Analysis – 2026-07-26 Batch 2

**Repo:** `AnuragRamdasan/fond-reel-masters`
**Analyst:** Parallel Loop AI
**Date:** 2026-07-26
**Scope:** Code review of the post-fix codebase after Bugs A–L were closed.

---

## Summary

Three new bugs were identified by reviewing the current `main` branch after all Bugs A–L were applied. None are regressions; all were latent before the fix batch.

| ID | File | Severity | Status |
|----|------|----------|--------|
| M | `tools/verify_protagonist.py` | MEDIUM | Fixed in this PR |
| N | `tools/verify_protagonist.py` | LOW | Fixed in this PR |
| O | `tools/verify_integrity.py` | HIGH | Fixed in this PR |

---

## Bug M — `find_parts_in_dir` returns the same list as both elements of its 2-tuple (MEDIUM)

### Location
`tools/verify_protagonist.py` — `find_parts_in_dir()`, all three `return` sites.

### Root Cause
The function signature promises a `Tuple[List[Path], List[Path]]` and the docstring says  
*"Returns (all_parts, all_parts) so caller can reassemble and sample at any timestamp."*  
Both positions of the tuple hold **the exact same Python list object**. The caller always unpacks as `all_parts, _ = find_parts_in_dir(...)` and discards the second element, so the duplicate never helps. The misleading signature adds cognitive overhead and invites future callers to mistakenly assume the two lists are distinct (e.g. one ordered by assembly, one by sampling index).

### Impact
No runtime crash — the discard `_` makes this safe today. However, any future caller that assigns both return values and mutates one would silently mutate the other, leading to incorrect part ordering for assembly or frame extraction.

### Fix
Change the return type to `List[Path]` (a single list), update the three `return` statements, update the caller unpacking `all_parts, _ = …` → `all_parts = …`, and correct the docstring.

---

## Bug N — `extract_frame_bytes` temp-file descriptor leak when `NamedTemporaryFile` raises mid-`with` block (LOW)

### Location
`tools/verify_protagonist.py` — `extract_frame_bytes()`.

### Root Cause
The function guards `tmp_path` with an `if tmp_path is not None` check in the `finally` block, which correctly handles the case where the `NamedTemporaryFile(…)` call itself raises before assignment. However, the file is opened with `delete=False`, meaning the OS file descriptor is held open for the duration of the `with` block. If `tmp.name` assignment or any code between the `with` entry and `os.unlink(tmp_path)` raises an unexpected exception that bypasses the `finally` (e.g., a `BaseException` like `KeyboardInterrupt` or `SystemExit`), the file on disk is **never deleted**.

Additionally, using `NamedTemporaryFile` with `delete=False` inside a `try/finally` that calls `os.unlink` is the manually-managed equivalent of what `tempfile.TemporaryFile` or a context manager provides automatically. The current pattern is unnecessarily fragile.

### Impact
Orphaned `.jpg` temp files accumulate in `/tmp` on the CI runner. On long CI runs or machines with small `/tmp`, this can cause disk exhaustion. On most runs the `finally` fires correctly, so this is a low-frequency issue.

### Fix
Replace the `NamedTemporaryFile(delete=False)` + manual `os.unlink` pattern with a `tempfile.TemporaryDirectory()` context manager wrapping a named path, which guarantees cleanup regardless of exception type.

---

## Bug O — `scan_repo` in `verify_integrity.py` silently skips `audition/`, `drafts/`, `covers/`, and `screen-inserts/` directories (HIGH)

### Location
`tools/verify_integrity.py` — `scan_repo()`.

### Root Cause
The scan loop hard-codes only two parent directories to descend into:

```python
elif item.is_dir() and item.name in ("ads-bridge", "masters"):
    for sub in item.iterdir():
        if sub.is_dir():
            dirs_to_check.append(sub)
```

The repository root also contains `audition/`, `covers/`, `drafts/`, and `screen-inserts/` directories, each of which holds archive content with manifest files. These are **never visited** by `scan_repo()` or the `--all` flag. Any integrity problem (missing manifest, SHA256 mismatch, corrupt part) in those directories produces no CI failure and no QA report.

### Impact
**High.** The entire `audition/`, `covers/`, `drafts/`, and `screen-inserts/` trees are unprotected by integrity checks. A corrupt or tampered file in any of those directories would pass CI silently — exactly the failure mode that Bug #1 was originally introduced to prevent.

### Fix
Replace the hard-coded `("ads-bridge", "masters")` allowlist with a dynamic approach: for any top-level directory that is **not** a date-named directory and is **not** in a known-skip list (`.github`, `tools`, `docs`, `qa`), descend into its subdirectories and add them to the scan list.

---

## Files Changed

- `tools/verify_protagonist.py` — Bugs M and N
- `tools/verify_integrity.py` — Bug O
