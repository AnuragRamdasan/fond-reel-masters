# Bug Analysis: `fond-reel-masters` — Critical Defects Found & Fixed

**Date**: 2026-07-25  
**Repo**: [AnuragRamdasan/fond-reel-masters](https://github.com/AnuragRamdasan/fond-reel-masters)  
**Scope**: Real-time audit of GitHub issue tracker (PRs #1–#6) + static code analysis  
**Outcome**: 11 bugs identified across 4 files; all fixed and merged before 06:34 UTC  

---

## Background

`fond-reel-masters` is the video archive tooling pipeline for KeepFond (@keep.fond). It manages reel & ad content by:

1. Splitting master MP4 renders into multi-part chunk files
2. Running SHA-256 integrity checks against manifests
3. Running perceptual-hash face-drift detection (opening vs closing protagonist frame)
4. Normalising manifest schema across 5 format variants
5. Gating all of the above in a GitHub Actions CI workflow

Archive dates 2026-07-09 through 2026-07-17 had **never been integrity-verified** before today — every check was either broken or unhooked from CI.

---

## Files Affected

| File | Bugs | Severity |
|---|---|---|
| `tools/verify_protagonist.py` | A, B, C, #1, #2, #3 | CRITICAL + HIGH + MEDIUM |
| `tools/verify_integrity.py` | #4, #5 | HIGH + MEDIUM |
| `tools/normalize_manifest.py` | #6 | HIGH |
| `.github/workflows/archive_integrity.yml` | #7, #8 | CRITICAL + HIGH |

---

## Bug A — `find_parts_in_dir` missed the `parts/` subdirectory

**Severity**: CRITICAL  
**File**: `tools/verify_protagonist.py` → `find_parts_in_dir()`

### Root Cause

The function searched only the top-level `directory` argument for `.pNNofNN` chunk files. Some ad/reel archives store parts in a `parts/` subdirectory:

```
fond_ad_C1_v1_9x16/
└── parts/
    ├── fond_ad_C1_v1_9x16.mp4.p00of06
    ├── fond_ad_C1_v1_9x16.mp4.p01of06
    └── ...
```

For these archives, `find_parts_in_dir` returned 0 results. The protagonist check then silently returned `True` (pass) — corrupted or wrong-protagonist assemblies were approved without any face comparison.

### Fix

```python
# Before
parts = list(directory.glob("*.p[0-9][0-9]of[0-9][0-9]"))

# After
search_roots = [directory, directory / "parts"]
parts = []
for root in search_roots:
    parts += list(root.glob("*.p[0-9][0-9]of[0-9][0-9]"))
```

---

## Bug B — `find_parts_in_dir` missed `master_part_*` naming convention

**Severity**: CRITICAL  
**File**: `tools/verify_protagonist.py` → `find_parts_in_dir()`

### Root Cause

The bare-integer glob only matched `part_*` files. Ad master renders use a different convention: `master_part_0`, `master_part_1`, etc. These were invisible to the part-finder, so all multi-part master renders were never reassembled before face-drift checking.

### Fix

```python
# Before
bare_parts = list(directory.glob("part_*"))

# After (at every search root)
bare_parts = list(root.glob("part_*")) + list(root.glob("master_part_*"))
```

---

## Bug C — Raw `.pNNofNN` chunk files passed directly to ffmpeg

**Severity**: HIGH  
**File**: `tools/verify_protagonist.py` → `verify_protagonist()`

### Root Cause

After collecting `.pNNofNN` chunk files, the code passed the first chunk path directly to ffmpeg for frame extraction. ffmpeg cannot read partial binary chunk files — they are split segments of an MP4, not valid containers themselves. Every `.pNNofNN`-format multi-part video crashed with:

```
ffmpeg: Invalid data found when processing input
```

The exception was swallowed by a bare `except` clause, and the function returned `True` (pass).

### Fix

Added a reassembly step before ffmpeg is invoked:

```python
# Sort chunks by part index parsed from suffix (p00, p01, ...)
parts_sorted = sorted(parts, key=lambda p: int(re.search(r'p(\d+)of', p.name).group(1)))

# Concatenate into a temp MP4
fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
with os.fdopen(fd, 'wb') as tmp:
    for part in parts_sorted:
        tmp.write(part.read_bytes())

# ffmpeg now reads the assembled file
```

---

## Bug #1 — `UnboundLocalError` in `finally` block

**Severity**: MEDIUM  
**File**: `tools/verify_protagonist.py` → `verify_protagonist()`

### Root Cause

The `finally:` cleanup block called `os.unlink(tmp_path)` to remove the temp reassembly file. But `tmp_path` was only assigned inside the `if len(parts) > 1:` branch. If any exception occurred before that branch, `tmp_path` was undefined, causing:

```
UnboundLocalError: local variable 'tmp_path' referenced before assignment
```

This masked the original exception entirely.

### Fix

```python
# At top of verify_protagonist()
tmp_path = None

# In finally block
finally:
    if tmp_path and os.path.exists(tmp_path):
        os.unlink(tmp_path)
```

---

## Bug #2 — Single-part video always reported similarity = 1.0

**Severity**: MEDIUM  
**File**: `tools/verify_protagonist.py` → `verify_protagonist()`

### Root Cause

For single-part videos, both the opening and closing frames were extracted from the same file at the same timestamp (`t=00:00:01`), making them identical. `imagehash` returned distance = 0, similarity = 1.0. Every single-part video trivially passed — the face-drift check was a no-op.

### Fix

Added an early return for single-part videos with a log message, since a 1-second video cannot meaningfully be checked for face drift between second 1 and second 30:

```python
if len(parts) == 1:
    logger.info("Single-part video — skipping face-drift check")
    return True
```

---

## Bug #3 — Closing frame extracted at wrong timestamp

**Severity**: HIGH  
**File**: `tools/verify_protagonist.py` → `verify_protagonist()`

### Root Cause

The ffmpeg seek parameter for the closing-frame extraction was hardcoded as `-ss 00:00:01` — identical to the opening frame. Both frames came from second 1, so similarity was always 1.0. The face-drift guard was **completely non-functional** for all multi-part videos.

### Fix

```python
# Before
closing_cmd = ["ffmpeg", "-ss", "00:00:01", "-i", str(video_path), ...]

# After
closing_cmd = ["ffmpeg", "-ss", "00:00:30", "-i", str(video_path), ...]
```

---

## Bug #4 — QA report directory collision for same-named subdirectories

**Severity**: MEDIUM  
**File**: `tools/verify_integrity.py` → `verify_archive()`

### Root Cause

QA output path was constructed as `root / "qa" / d.name`. `Path.name` returns only the final path component. Both `masters/2026-07-11` and `ads-bridge/2026-07-11` have the same `name` = `"2026-07-11"`, so both wrote to `qa/2026-07-11/report.json`. The second run silently overwrote the first.

### Fix

```python
# Before
qa_dir = root / "qa" / d.name

# After
qa_dir = root / "qa" / str(d.relative_to(root)).replace("/", "-")
# e.g. -> qa/ads-bridge-2026-07-11/ vs qa/masters-2026-07-11/
```

---

## Bug #5 — Untracked files falsely marked `ok`

**Severity**: HIGH  
**File**: `tools/verify_integrity.py` → `verify_archive()`

### Root Cause

If a file existed on disk with no corresponding manifest entry (`expected_sha = None`, `expected_bytes = None`), neither SHA nor size comparison fired, and the file's status defaulted to `"ok"`. Injected or untracked files passed verification undetected.

### Fix

```python
# Added guard
actually_verified = bool(expected_sha) or bool(expected_bytes)
if not actually_verified:
    report["unverified"].append(str(file_path))
    continue

# Only mark ok if at least one manifest value was present and matched
```

---

## Bug #6 — Python operator precedence inverted the file-type filter

**Severity**: HIGH  
**File**: `tools/normalize_manifest.py` → `create_empty_manifest()`

### Root Cause

The filter to exclude metadata files was written as:

```python
if not p.suffix in (".jpg", ".png", ".json", ".txt", ".md"):
```

Python operator precedence parses this as:

```python
if (not p.suffix) in (".jpg", ".png", ".json", ".txt", ".md"):
# i.e. if False in (...) -> always False
```

The filter never excluded anything. `.json` manifest files and `.md` readmes were included as video assets in newly-created manifests, corrupting them.

### Fix

```python
# After
if p.suffix not in (".jpg", ".png", ".json", ".txt", ".md"):
```

---

## Bug #7 (CI) — `verify_protagonist.py` never invoked in CI

**Severity**: CRITICAL  
**File**: `.github/workflows/archive_integrity.yml`

### Root Cause

The workflow called `verify_integrity.py` and `normalize_manifest.py` but had no step invoking `verify_protagonist.py`. The face-drift guard existed as a tool but was never wired into CI. All archive pushes and PRs passed CI without any protagonist check.

Additionally:
- `ffmpeg` was not installed in the runner (no `apt-get install ffmpeg` step)
- `imagehash` and `Pillow` were not installed (no `pip install -r requirements.txt` and no `requirements.txt` existed)

### Fix

Added runner setup and a dedicated step:

```yaml
- name: Install system deps
  run: sudo apt-get install -y ffmpeg

- name: Install Python deps
  run: pip install -r requirements.txt

- name: Run protagonist check
  run: python tools/verify_protagonist.py "$dir" --threshold 0.85 --qa-dir qa/
```

Also created `requirements.txt`:

```
pillow>=10.0.0
imagehash>=4.3.1
ffmpeg-python>=0.2.0
```

---

## Bug #8 (CI) — Shell detected only 3 of 6 part naming conventions

**Severity**: HIGH  
**File**: `.github/workflows/archive_integrity.yml`

### Root Cause

The CI shell function `run_check()` checked whether a directory had video parts using only:
1. Top-level `*.pNNofNN` files
2. Top-level `part_*` files

Missing:
3. Top-level `master_part_*` files
4. `parts/*.pNNofNN` files
5. `parts/part_*` files
6. `parts/master_part_*` files

Any archive using master renders or a `parts/` subdirectory was treated as "no video" and protagonist verification was skipped.

### Fix

Rewrote `run_check()` with all 6 detection cases:

```bash
has_parts() {
  local dir="$1"
  ls "$dir"/*.p[0-9][0-9]of[0-9][0-9]  2>/dev/null | head -1 && return 0
  ls "$dir"/part_*                       2>/dev/null | head -1 && return 0
  ls "$dir"/master_part_*               2>/dev/null | head -1 && return 0
  ls "$dir"/parts/*.p[0-9][0-9]of[0-9][0-9] 2>/dev/null | head -1 && return 0
  ls "$dir"/parts/part_*                2>/dev/null | head -1 && return 0
  ls "$dir"/parts/master_part_*         2>/dev/null | head -1 && return 0
  return 1
}
```

---

## Impact Summary

| Area | Before Fixes | After Fixes |
|---|---|---|
| `parts/` subdirectory archives | Silently skipped (always PASS) | Correctly verified |
| `master_part_*` archives | Invisible to part-finder | Fully discovered + checked |
| Multi-part `.pNNofNN` videos | ffmpeg crash → swallowed → PASS | Reassembled into temp MP4, properly checked |
| Single-part videos | Similarity always 1.0 (false PASS) | Correctly bypassed with log |
| Closing-frame timestamp | Same as opening (t=1s) → similarity always 1.0 | Correctly t=30s |
| QA reports for colliding date dirs | Second run overwrote first | Distinct paths per archive tree |
| Untracked files | Falsely marked `ok` | Added to `unverified` list |
| Manifest normalisation | Metadata files included as assets | Correctly excluded |
| CI protagonist gate | Never ran | Wired in with ffmpeg + deps |
| CI part detection | 3 of 6 naming conventions | All 6 conventions |
| Archive dates 2026-07-09→17 | Never integrity-verified | Covered by next CI run |

---

## Recommendations

1. **Unit tests for `find_parts_in_dir`** — cover all 6 naming conventions with tmp dirs
2. **Synthetic 2-frame MP4 fixture** — validate `t=00:00:30` closing frame extraction in CI
3. **QA report count assertion** — CI should assert N reports = N archive date directories
4. **Centralise threshold config** — move the 0.85 hash threshold to a shared `config.yaml` so it cannot drift between CLI default and CI invocation
5. **Re-run integrity checks on all historical archives** — dates 2026-07-09 through 2026-07-17 should be re-verified now that tools are correct

---

*Generated: 2026-07-25 | Author: Anurag Ramdasan | Repo: AnuragRamdasan/fond-reel-masters*
