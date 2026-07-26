# Bug Analysis — KeepFond CI/Archive Integrity (2026-07-26, Final)

**Repository:** `fond-reel-masters`  
**Analysis date:** 2026-07-26  
**Session:** Parallel Loop AI — automated real-time bug triage  
**Status:** All 3 bugs fixed and pushed to `fix/bugs-g-h-i-consolidated`

---

## Overview

Four previous PRs (#10, #11, #12, #13) were opened against these same bugs but none merged to `main`. This PR is a clean consolidation that applies all three fixes to the current `main` HEAD.

---

## Bug G — CRITICAL: Sub-30s reels silently bypass face-identity check

**File:** `tools/verify_protagonist.py`  
**Severity:** 🔴 CRITICAL  
**Status:** Fixed in this PR

### Root Cause

The closing frame timestamp was hardcoded to `"00:00:30"`:

```python
print("  🎞️   Extracting closing frame (t=00:00:30)…")
closing_frame = extract_frame_bytes(assembled_path, "00:00:30")
```

For any reel shorter than 30 seconds, `ffmpeg -ss 00:00:30` seeks past the end of the file and `extract_frame_bytes()` returns `None`. The caller then hit the combined guard:

```python
if opening_frame is None or closing_frame is None:
    print("  ⚠️   Could not extract frames (ffmpeg not available or file corrupt)")
    print("  ✅  Check skipped (no ffmpeg) — treating as PASS")
    return True  # SILENT PASS
```

This produces the misleading "ffmpeg not available" message — even when ffmpeg is working perfectly — and returns `True` (PASS). **Every sub-30-second reel silently bypassed protagonist identity checking.**

### Impact

Typical Veo clip length is 6–8 seconds. A 3-clip reel (~20 seconds total) is the most common production case. This bug affected the majority of production reels.

### Fix

1. Added `get_video_duration()` using `ffprobe` to probe the actual assembled video duration.
2. Computing closing timestamp dynamically: `max(1s, min(duration − 1s, 30s))`
3. Split the combined `or` guard into two distinct diagnostic branches with accurate error messages.
4. Added `probed_duration_seconds` field to the QA JSON report.
5. Updated contact sheet label to reflect the real closing timestamp.

---

## Bug H — HIGH: `part_*` naming convention produces empty parts list

**File:** `tools/normalize_manifest.py`  
**Severity:** 🟠 HIGH  
**Status:** Fixed in this PR

### Root Cause

`normalise_reel_manifest()` only globbed `master_part_*`:

```python
part_files = sorted(directory.glob("master_part_*"))
```

Directories from the 2026-07-09 era use the bare naming convention `part_0`, `part_1`, `part_2`… The glob returns an empty list `[]`, so the emitted manifest has:

```json
{
  "parts": 0,
  "parts_detail": []
}
```

### Impact

Downstream `verify_integrity.py` sees `MISSING PARTS` or `UNTRACKED` failures on every 2026-07-09 era directory — systematic false CI failures that mask real issues and add noise to the CI log.

### Fix

Glob both `master_part_*` and `part_*`, deduplicate by filename, sort by name:

```python
seen: Dict[str, Path] = {}
for p in list(directory.glob("master_part_*")) + list(directory.glob("part_*")):
    seen[p.name] = p
part_files = sorted(seen.values(), key=lambda p: p.name)
```

---

## Bug I — LOW: Dead `sha256_of_bytes()` function

**File:** `tools/verify_integrity.py`  
**Severity:** 🟡 LOW  
**Status:** Fixed in this PR

### Root Cause

```python
def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

This function was defined but **never called**. All hashing in the codebase uses the streaming `sha256_of_parts()` which reads files in 64 KB chunks.

### Impact

- **OOM risk:** If `sha256_of_bytes()` were ever called on a multi-GB video file, it would load the entire file into memory, likely causing an OOM kill in the CI environment.
- **Dead code noise:** Adds confusion for future contributors who may not know which function to use for hashing.
- **Static analysis noise:** Linters flag unused functions.

### Fix

Removed the function entirely. Added a comment in the module header explaining the removal.

---

## Stale PRs Superseded

| PR | Title | Status |
|----|-------|--------|
| #10 | docs: add bug analysis — CI/archive integrity audit (2026-07-25) | Open, unmerged |
| #11 | fix: 3 new bugs (G/H/I) — short-reel silent PASS, part_* manifest gap, dead hasher | Open, unmerged |
| #12 | fix: bugs G/H/I — short-reel silent PASS, part_* manifest gap, dead hasher | Open, unmerged |
| #13 | fix: Bugs G/H/I — silent protagonist bypass, missing part_* glob, dead hash function | Open, unmerged |

This PR (`fix/bugs-g-h-i-consolidated`) applies all fixes cleanly to the current `main` HEAD and supersedes all of the above.

---

## Test Plan

- [ ] Run `python tools/verify_protagonist.py --dir <sub-30s-reel-dir>` — confirm closing timestamp is probed dynamically and `probed_duration_seconds` appears in the QA JSON report
- [ ] Confirm protagonist check runs to completion (not silently skipped) on a 6–8s Veo clip reel
- [ ] Run `python tools/normalize_manifest.py --dir <2026-07-09-era-dir>` — confirm `parts_detail` is non-empty and lists both `part_*` files
- [ ] Run `python tools/verify_integrity.py --dir <2026-07-09-era-dir>` — confirm no spurious MISSING PARTS / UNTRACKED failures
- [ ] Confirm `sha256_of_bytes` is gone from `verify_integrity.py` and no import references remain
- [ ] Run `python tools/verify_integrity.py --all` on the full repo — confirm clean exit
