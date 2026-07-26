# Bug Analysis — fond-reel-masters — 2026-07-26

Analyst: Parallelloop AI Agent (session ee2a88d5)
Repo: AnuragRamdasan/fond-reel-masters
Date: 2026-07-26

---

## Summary

Three bugs were identified from open GitHub PRs (#10, #11, #12) on the
`fond-reel-masters` CI/archive-integrity toolchain. All three bugs existed on the
`main` branch as of commit `1fdbdf6185e89dba246c9893ad7b94c9cf9601a7`. None of the
prior fix PRs had been merged. This document records the root-cause analysis and
the fixes implemented on branch `fix/bugs-G-H-I-parallelloop-2026-07-26`.

---

## Bug G — CRITICAL — Silent bypass of protagonist identity check for short reels

**File:** `tools/verify_protagonist.py`
**Function:** `verify_protagonist()`

### Root Cause

The closing frame was always extracted at a hardcoded timestamp of `"00:00:30"`:

```python
print("  🎞️   Extracting closing frame (t=00:00:30)…")
closing_frame = extract_frame_bytes(assembled_path, "00:00:30")
```

For any reel whose total assembled duration is shorter than 30 seconds, `ffmpeg -ss
00:00:30` seeks past the end of the file and returns no frames. The `extract_frame_bytes()`
function catches the non-zero return code and returns `None`.

The caller then evaluated both frame results with a single combined `or` guard:

```python
if opening_frame is None or closing_frame is None:
    print("  ⚠️   Could not extract frames (ffmpeg not available or file corrupt)")
    print("  ℹ️   Install ffmpeg to enable face-identity checking")
    print("  ✅  Check skipped (no ffmpeg) — treating as PASS")
    return True
```

Because `closing_frame is None` evaluates to `True`, the entire protagonist check is
short-circuited and returns `True` (PASS) — even though ffmpeg is fully installed and
the opening frame was extracted successfully. The misleading "ffmpeg not available"
message disguises the real problem completely.

### Impact

Every reel with a total assembled duration shorter than 30 seconds silently bypasses
protagonist identity verification. A different face appearing in the closing segment
(e.g., due to Veo A vs Veo B using different character seeds) would not be caught.
Given typical Veo clip lengths of 6–8 seconds each, a 3-clip reel assembled to
~20 seconds is a very common case.

Severity: **CRITICAL** — the check is the primary defence against cross-seed identity
drift in multi-clip reels. Silent PASS defeats its entire purpose.

### Fix

1. Added `get_video_duration(video_path)` — calls `ffprobe` with `-show_entries
   format=duration` to obtain the actual assembled video duration in seconds.
2. Computed closing timestamp dynamically:
   `closing_ts = max(1s, min(duration − 1s, 30s))`
   This clamps the seek point between 1 second (minimum meaningful frame) and 30
   seconds (previous cap), always landing within the file.
3. Split the combined `or` guard into two distinct branches — one for
   `opening_frame is None`, one for `closing_frame is None` — with accurate
   diagnostic messages for each case.
4. Added `probed_duration_seconds` field to the QA JSON report so operators can see
   the measured duration alongside the similarity score.
5. Updated the contact sheet label to show the actual `closing_ts` instead of the
   hardcoded `"00:00:30"` string.

**Commit:** `4e22c4c0ed449e8fa92cf0be6034e40904330b0d`

---

## Bug H — HIGH — Manifest normaliser ignores `part_*` naming convention

**File:** `tools/normalize_manifest.py`
**Function:** `normalise_reel_manifest()`

### Root Cause

The function only globbed for files matching `master_part_*`:

```python
def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    """Schema 1a → Schema 2."""
    part_files = sorted(directory.glob("master_part_*"))
    parts_info = []
    for pf in part_files:
        parts_info.append({
            "file": pf.name,
            "bytes": pf.stat().st_size,
        })
```

Directories from the 2026-07-09 era use a bare `part_0`, `part_1`, `part_2` naming
convention (without the `master_` prefix). The glob `master_part_*` returns an empty
list for these directories, so `parts_info` is `[]` and the emitted manifest contains
`"parts": 0` and `"parts_detail": []`.

### Impact

On the next CI run, `verify_integrity.py` reads the normalised manifest, finds
`parts: 0` expected but sees the actual part files on disk, and reports either
`MISSING PARTS` (if the manifest entry count is compared to disk files) or
`UNTRACKED` (if the part files have no corresponding manifest entry). The false
failure causes CI to block merges for directories that are actually valid. In the
worst case, an operator re-runs normalisation to "fix" the manifest, discarding
the real SHA256 checksums that were there before.

Severity: **HIGH** — causes systematic false CI failures for all 2026-07-09 era
archive directories.

### Fix

Collect both naming conventions, deduplicate by filename (using a dict keyed on
`f.name`), and sort by name:

```python
seen = {}
for pf in list(directory.glob("master_part_*")) + list(directory.glob("part_*")):
    if pf.is_file() and pf.name not in seen:
        seen[pf.name] = pf
part_files = sorted(seen.values(), key=lambda p: p.name)
```

The deduplication step ensures that a directory which happens to have both
`master_part_01` and `part_01` present (e.g., during a migration) does not
double-count entries.

**Commit:** `a1714194815b2e7b5fa7255c18b66ee6441e5e7a`

---

## Bug I — LOW — Dead function `sha256_of_bytes()` in verify_integrity.py

**File:** `tools/verify_integrity.py`
**Function:** `sha256_of_bytes(data: bytes) -> str` (dead code)

### Root Cause

The function was defined but never called anywhere in the codebase:

```python
def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

All actual hashing in the module is done via `sha256_of_parts(part_paths)`, which
reads files in 64 KB chunks to avoid loading entire video files into memory. The
`sha256_of_bytes` function implies an in-memory approach that is not used and
would be unsafe for large files.

### Impact

The dead function creates ambiguity: a future developer adding a new code path might
reach for `sha256_of_bytes` and accidentally load a multi-GB video file into memory,
causing an OOM crash on CI runners. Additionally, static analysis tools flag dead
code as a code-quality violation, adding noise to reviews.

Severity: **LOW** — no runtime impact; risk is future confusion and potential misuse.

### Fix

Remove the function entirely. All callers (zero currently) are unaffected. The
`sha256_of_parts` function remains and is the correct approach for streaming
file hashing.

**Commit:** `70e2fd7c7c616c7feac724c8366bc693174c2fe6`

---

## Branch and PR

- **Fix branch:** `fix/bugs-G-H-I-parallelloop-2026-07-26`
- **Base branch:** `main`
- **Commits:**
  - `4e22c4c` — fix(Bug G): dynamic closing timestamp via ffprobe for sub-30s reels
  - `a171419` — fix(Bug H): glob both master_part_* and part_* in normalise_reel_manifest
  - `70e2fd7` — fix(Bug I): remove dead sha256_of_bytes() function
  - `(this doc)` — docs: add bug analysis note for Bugs G, H, I

---

*Analysis generated by Parallelloop AI Agent on 2026-07-26*
