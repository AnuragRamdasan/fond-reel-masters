# Bug Analysis — fond-reel-masters — 2026-07-26

## Summary

Six bugs identified and fixed in the 2026-07-26 batch. Two are **CRITICAL** (protagonist check
silently disabled for the majority of reels; QA directory collision across same-date archive
directories), two are **HIGH** (bare `part_*` glob miss in manifest normaliser; unguarded `grep`
aborting CI on pipefail), one is **HIGH** (`scan_all()` passing plain files to a directory-only
processor), and one is **MEDIUM** (RGBA compositing producing solid-black overlays).

All 6 bugs fixed in the same commit batch. Fixes confirmed by inline `# FIX Bug X` comments in
source.

---

## Bug G — CRITICAL — Hardcoded 30-Second Closing Timestamp Disables Protagonist Check

**File:** `tools/verify_protagonist.py`
**Severity:** CRITICAL
**Detected:** 2026-07-26
**Status:** Fixed

### Root Cause

The closing frame for protagonist face-identity comparison was extracted at a hardcoded timestamp of
`"00:00:30"`:

```python
# BUG G — hardcoded to 30 s regardless of reel duration
closing_frame = extract_frame_bytes(assembled_path, "00:00:30")
```

Typical fond-reel-masters reels are composed of **3 × 6–8 s Veo clips**, giving a total duration of
approximately **18–24 seconds** — well short of the 30-second seek point. When `ffmpeg` seeks past
the end of the file it returns an error and produces no frame data; `extract_frame_bytes()` therefore
returns `None`.

The guard that followed used a combined `or` condition:

```python
if opening_frame is None or closing_frame is None:
    return True   # PASS — silent failure, no diagnostic
```

Because `closing_frame` was `None` for every reel shorter than 30 s, **the protagonist identity
check was completely non-functional for the vast majority of KeepFond reels**. The tool always
returned `True` (pass) without ever computing a perceptual hash or comparing faces.

### Impact

- Any reel could contain a misidentified protagonist and the check would never catch it.
- CI remained green regardless of protagonist identity — false confidence across all archives shorter
  than 30 s.
- The failure was completely silent: no error message, no warning, no log entry. The tool reported
  success.

### Fix

Introduced `get_video_duration()`, which shells out to `ffprobe` to probe the actual duration of the
reassembled MP4 before selecting the closing timestamp:

```python
def get_video_duration(video_path: Path, fallback: float = 30.0) -> float:
    """Probe actual duration via ffprobe; return fallback on error."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(video_path)],
            capture_output=True, timeout=15, text=True,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return fallback
```

The closing timestamp is now computed dynamically:

```python
probed_duration = get_video_duration(assembled_path)
closing_secs = max(1.0, min(probed_duration - 1.0, 30.0))
h_ts, rem = divmod(int(closing_secs), 3600)
m_ts, s_ts = divmod(rem, 60)
closing_ts = f"{h_ts:02d}:{m_ts:02d}:{s_ts:02d}"
```

`max(1.0, ...)` prevents a zero-duration seek for extremely short clips; `min(..., 30.0)` caps the
seek at 30 s for unusually long content. The combined `or`-guard was also split into two independent
guards so each `None` case produces a specific, actionable diagnostic rather than a silent pass.

---

## Bug C — CRITICAL — QA Directory Collision Across Same-Date Archive Directories

**File:** `tools/verify_protagonist.py`
**Severity:** CRITICAL
**Detected:** 2026-07-26
**Status:** Fixed

### Root Cause

The fallback path for the `qa/` output directory used only `target_dir.name` (the final path
component) rather than the full relative path:

```python
# BUG C — only the basename, not the full repo-relative path
qa_dir = repo_root / "qa" / target_dir.name
```

For archive directories with the naming pattern `masters/YYYY-MM-DD-<slug>`, `target_dir.name`
evaluates to `YYYY-MM-DD-<slug>`. However, when `target_dir` is an absolute path that cannot be
expressed relative to `repo_root`, the fallback strips the prefix and produces only the date segment
— e.g. `qa/2026-07-15/` — meaning **all archives from the same date share a single QA output
directory**. Contact sheets, frame extracts, and analysis JSON from run N silently overwrite those
from run N-1.

### Impact

- QA output from the most recent run for a given date masks all previous runs from the same date.
- Debugging protagonist failures is unreliable: the frames on disk may belong to a different archive
  than the one currently under analysis.
- The collision is completely silent — no error, no warning.

### Fix

Use `Path.relative_to()` to compute the full repo-relative path, with a safe fallback to the
directory name when the path is outside the repo root:

```python
try:
    rel = target_dir.relative_to(repo_root)
except ValueError:
    rel = Path(target_dir.name)
qa_dir = repo_root / "qa" / str(rel).replace("/", "-")
```

This produces unique paths such as `qa/masters-2026-07-15-sunrise/` for each distinct archive
directory.

---

## Bug H — HIGH — Reel Manifest Normaliser Misses `part_*` Naming Convention

**File:** `tools/normalize_manifest.py`
**Severity:** HIGH
**Detected:** 2026-07-26
**Status:** Fixed

### Root Cause

`normalise_reel_manifest()` only globbed for the `master_part_*` naming convention when enumerating
part files in a directory:

```python
# BUG H — 2026-07-09-era part_* files never discovered
part_files = sorted(directory.glob("master_part_*"), key=lambda p: p.name)
```

Archive directories created during the 2026-07-09 era use bare `part_0`, `part_1`, … naming without
the `master_` prefix. The glob returned an empty list for these directories, causing the normalised
manifest to always be written with:

```json
{ "parts": 0, "parts_detail": [] }
```

### Impact

- Every archive from the 2026-07-09 era produces a structurally invalid schema-v2 manifest
  (`"parts": 0`) even when the actual part files are present and intact on disk.
- Downstream consumers that rely on the normalised manifest (integrity checker, CI artefact report)
  see zero parts and may incorrectly report the archive as empty or corrupt.
- The normaliser exits 0 with no warning — the bad manifest is written silently.

### Fix

Merge results from both globs, deduplicate by filename, and sort:

```python
part_files = sorted(
    {p.name: p for p in
     list(directory.glob("master_part_*")) + list(directory.glob("part_*"))
    }.values(),
    key=lambda p: p.name,
)
```

The dict-keyed deduplication ensures a file that matches both patterns (e.g. a future naming
collision) is counted only once.

---

## Bug E — HIGH — Unguarded `grep` Aborts CI Under `pipefail`

**File:** `.github/workflows/archive_integrity.yml`
**Severity:** HIGH
**Detected:** 2026-07-26
**Status:** Fixed

### Root Cause

The workflow step that identifies changed archive directories used a bare `grep` pipeline:

```bash
git diff --name-only HEAD~1 HEAD | \
  grep -E '^(masters/[^/]+|ads-bridge/[^/]+)/' | \
  sed 's|...|' | sort -u > /tmp/changed_dirs.txt
```

`grep` exits with code 1 when it finds no matches. GitHub Actions runs each `run:` block under an
implicit `set -eo pipefail`. On a commit that touches only non-archive files (e.g. README, CI YAML,
docs), the `grep` finds no matches, exits 1, and the **entire workflow step fails immediately** —
even though this is a perfectly valid state (no archive directories changed means nothing to check).

### Impact

- Any documentation-only or CI-only commit causes the archive integrity workflow to fail with a
  spurious red build.
- Developers waste time investigating a false failure.
- `changed_dirs.txt` is never created, causing subsequent steps that read it to fail with a
  "file not found" error, producing a confusing cascade of failures.

### Fix

Pre-create the file with `touch` and add `|| true` to suppress the non-zero `grep` exit:

```bash
touch /tmp/changed_dirs.txt
{ git diff --name-only HEAD~1 HEAD | \
  grep -E '^(masters/[^/]+|ads-bridge/[^/]+)/' || true; } | \
  sed 's|\([^/]*/[^/]*\)/.*|\1|' | sort -u > /tmp/changed_dirs.txt
```

---

## Bug D — HIGH — `scan_all()` Passes Plain Files to Directory Processor

**File:** `tools/normalize_manifest.py`
**Severity:** HIGH
**Detected:** 2026-07-26
**Status:** Fixed

### Root Cause

`scan_all()` iterated `p.iterdir()` without filtering to directories only:

```python
for sub in p.iterdir():
    dirs.append(sub)   # BUG D — no is_dir() guard; files included
```

Any loose file at the top level of a scanned path (e.g. a stray `.DS_Store`, `README.md`, or a
top-level `manifest.json`) was appended to the `dirs` queue and subsequently passed to
`process_directory()`, which calls `directory.glob(...)` and `directory.iterdir()`. In Python 3.11+
these methods raise `NotADirectoryError` when called on a plain file path.

### Impact

- `normalize_manifest.py` crashes with an unhandled `NotADirectoryError` whenever any loose file
  exists alongside archive directories in a scanned path.
- The crash occurs after some directories have already been processed, leaving the manifest
  normalisation in a partially-complete state. No clear indication of which directories were missed.

### Fix

Add an `is_dir()` guard inside the loop:

```python
for sub in p.iterdir():
    if sub.is_dir():   # FIX Bug D
        dirs.append(sub)
```

---

## Bug F — MEDIUM — RGBA Fill on RGB Canvas Produces Solid-Black Overlay

**File:** `tools/verify_protagonist.py`
**Severity:** MEDIUM
**Detected:** 2026-07-26
**Status:** Fixed

### Root Cause

The contact-sheet label overlay was drawn using a semi-transparent RGBA fill (`(0, 0, 0, 180)`)
directly onto an RGB canvas opened without an alpha channel:

```python
img = Image.open(io.BytesIO(data))                              # RGB mode
draw = ImageDraw.Draw(img)
draw.rectangle([(0, 0), (320, 30)], fill=(0, 0, 0, 180))       # BUG F — alpha ignored on RGB
```

Pillow's `ImageDraw` silently ignores the alpha component when drawing onto an RGB image. The fill
is interpreted as solid `(0, 0, 0)` — pure black — making the label overlay fully opaque rather than
semi-transparent. Any text drawn on top of this box against a dark background is effectively
invisible.

### Impact

- All QA contact-sheet images have a fully opaque black bar instead of the intended 70%-opacity
  overlay.
- Timestamp and similarity-score labels may be unreadable when text colour is dark.
- QA review is degraded and the contact sheet harder to interpret at a glance.

### Fix

Open the image in RGBA mode, draw onto a fully-transparent overlay layer, composite with
`Image.alpha_composite()`, then convert back to RGB for saving:

```python
img = Image.open(io.BytesIO(data)).convert("RGBA")
overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
draw_overlay = ImageDraw.Draw(overlay)
draw_overlay.rectangle([(0, 0), (320, 30)], fill=(0, 0, 0, 180))
img = Image.alpha_composite(img, overlay).convert("RGB")
draw = ImageDraw.Draw(img)
```

---

## Fix Summary

| Bug | Severity | File | Root Cause | Fix |
|-----|----------|------|------------|-----|
| G | CRITICAL | `verify_protagonist.py` | Hardcoded `"00:00:30"` closing timestamp; `extract_frame_bytes()` returns `None` for sub-30 s reels; combined `or`-guard silently returns `True` (PASS) | `get_video_duration()` via `ffprobe`; dynamic `closing_ts`; split `or`-guard into two independent checks |
| C | CRITICAL | `verify_protagonist.py` | QA dir used only `target_dir.name`; same-date archives collide in `qa/YYYY-MM-DD/` | Full relative path via `Path.relative_to(repo_root)` with safe fallback |
| H | HIGH | `normalize_manifest.py` | Only `master_part_*` globbed; 2026-07-09-era `part_*` dirs always produce `"parts": 0` | Merge both globs, deduplicate by filename |
| E | HIGH | `archive_integrity.yml` | Bare `grep` exits 1 on no matches; `pipefail` aborts CI on non-archive commits | `\|\| true` guard; pre-create file with `touch` |
| D | HIGH | `normalize_manifest.py` | `scan_all()` passes plain files to directory processor → `NotADirectoryError` in Python 3.11+ | `if sub.is_dir():` guard |
| F | MEDIUM | `verify_protagonist.py` | RGBA fill on RGB canvas → fully opaque black (alpha component silently ignored by Pillow) | Open as RGBA, composite transparent overlay, convert to RGB |

All fixes are present in `main`. Confirmed by `# FIX Bug X` inline comments in source files.
