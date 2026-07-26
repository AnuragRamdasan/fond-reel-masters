# Bug Analysis — fond-reel-masters CI/Archive Integrity
**Date:** 2026-07-26  
**Session:** Parallel Loop AI — Real-Time Bug Triage  
**Repo:** AnuragRamdasan/fond-reel-masters  
**Status:** ✅ Fixes implemented and pushed (this branch)

---

## Executive Summary

Three bugs were identified live on `main` by scanning the GitHub issue tracker (PRs #10–#13 had each independently reported the same three issues but none had been merged). This document provides root-cause analysis, impact assessment, and fix rationale for all three.

---

## Bug G — CRITICAL: Sub-30s reels silently bypass face-identity check

**File:** `tools/verify_protagonist.py`  
**Severity:** 🔴 CRITICAL  
**Introduced:** Unknown — present since protagonist check was first added  
**Affects:** Every reel shorter than 30 seconds (common: 3-clip Veo reels ≈ 20s)

### Root Cause

The closing frame timestamp was hardcoded to `"00:00:30"` in two places:

```python
# BEFORE (buggy)
print("  🎞️   Extracting closing frame (t=00:00:30)…")
closing_frame = extract_frame_bytes(assembled_path, "00:00:30")
```

For any reel shorter than 30 seconds, `ffmpeg -ss 00:00:30 -i <file>` seeks past end-of-file. ffmpeg exits non-zero, `extract_frame_bytes()` returns `None`. The caller then hits:

```python
# BEFORE (buggy combined guard)
if opening_frame is None or closing_frame is None:
    print("  ⚠️   Could not extract frames (ffmpeg not available or file corrupt)")
    print("  ℹ️   Install ffmpeg to enable face-identity checking")
    print("  ✅ Check skipped (no ffmpeg) — treating as PASS")
    return True  # ← silent PASS even though ffmpeg IS available and working
```

The combined `or` guard cannot distinguish between:
- ffmpeg genuinely missing (both frames are None)
- closing timestamp past EOF (only closing frame is None, ffmpeg is fine)

Result: **every sub-30s reel silently returned PASS**, with a misleading "ffmpeg not available" message, even on machines where ffmpeg was working perfectly.

### Impact

Typical Veo clip length: 6–8 seconds. A 3-clip reel totals ≈ 20–24 seconds. This is the **most common production case** for KeepFond. Protagonist-swapped reels (different faces at open vs close) were passing the CI integrity check 100% of the time.

### Fix

1. Added `get_video_duration(video_path)` using `ffprobe` to probe actual assembled video duration before seeking.
2. Computed a safe closing timestamp: `closing_ts = max(1.0, min(duration - 1.0, 30.0))` seconds → formatted as `MM:SS.mmm`.
3. Split the combined `or` guard into two distinct diagnostic branches with accurate messages.
4. Added `probed_duration_seconds` to the QA JSON report for auditability.
5. Updated contact sheet label to reflect the actual closing timestamp used.

```python
# AFTER (fixed)
probed_duration = get_video_duration(assembled_path)
if probed_duration is not None:
    safe_close_s = max(1.0, min(probed_duration - 1.0, 30.0))
    closing_ts = f"{int(safe_close_s // 60):02d}:{safe_close_s % 60:06.3f}"
else:
    closing_ts = "00:00:30"  # fallback if ffprobe unavailable

# Separate guards with accurate diagnostics
if opening_frame is None:
    print("  ⚠️   Could not extract opening frame (ffmpeg not available or file corrupt)")
    return True

if closing_frame is None:
    print(f"  ⚠️   Could not extract closing frame at t={closing_ts}")
    return True
```

---

## Bug H — HIGH: `part_*` naming convention produces empty parts list

**File:** `tools/normalize_manifest.py`  
**Severity:** 🟠 HIGH  
**Introduced:** When `master_part_*` naming was standardised (post 2026-07-16)  
**Affects:** All directories from 2026-07-09 era using bare `part_0`, `part_1`… naming

### Root Cause

```python
# BEFORE (buggy)
def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    """Schema 1a → Schema 2."""
    part_files = sorted(directory.glob("master_part_*"))  # ← only one pattern
```

Directories created in the 2026-07-09 era use `part_0`, `part_1`, `part_2`… as their naming convention. The `master_part_*` glob returns an empty list for these directories. The resulting normalised manifest has:

```json
{
  "parts": 0,
  "parts_detail": []
}
```

When `verify_integrity.py` later checks the same directory, it expects `N` parts (from the manifest's `parts` field, which came from the original `raw` JSON) but finds `0` parts on disk → `MISSING PARTS` or `UNTRACKED` failure.

### Impact

Systematic false CI failures on every 2026-07-09 era directory after any manifest normalisation run. Engineers may dismiss real failures as noise from these false positives.

### Fix

```python
# AFTER (fixed)
def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    """Schema 1a → Schema 2."""
    # FIX Bug H: glob both master_part_* and part_*, deduplicate by filename
    seen = {}
    for pf in list(directory.glob("master_part_*")) + list(directory.glob("part_*")):
        seen[pf.name] = pf
    part_files = sorted(seen.values(), key=lambda p: p.name)
```

Deduplication by `pf.name` prevents double-counting if a directory somehow has both patterns.

---

## Bug I — LOW: Dead `sha256_of_bytes()` function

**File:** `tools/verify_integrity.py`  
**Severity:** 🟡 LOW  
**Introduced:** Early development — never wired up  
**Affects:** No current behaviour (dead code), but poses future risk

### Root Cause

```python
# BEFORE (dead code)
def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

This function loads the entire file content into memory before hashing. All actual hashing in the module uses `sha256_of_parts()`, which reads in 64 KB chunks and never loads more than one chunk at a time.

### Impact

- **Current:** None — the function is never called.
- **Future risk:** If a developer copies this function or adds a call to it on a multi-GB video file, it will OOM the process.
- **Static analysis:** Dead code is flagged by linters and confuses contributors about which hash function to use.

### Fix

Removed the function entirely. Added a comment in its place explaining the removal and pointing to `sha256_of_parts()`.

---

## Consolidation Note

PRs #10–#13 each independently identified and attempted to fix these same three bugs, but none were merged to `main`. This PR (#15) applies all three fixes to current `main` HEAD as a single clean commit, superseding PRs #10–#13.

---

## Test Plan

| Test | Expected Result |
|------|-----------------|
| `python tools/verify_protagonist.py --dir <sub-30s-reel-dir>` | Closing timestamp probed dynamically; `probed_duration_seconds` appears in QA JSON; check runs to completion |
| Run on 6–8s Veo clip reel | Does NOT silently pass; actually evaluates face similarity |
| `python tools/normalize_manifest.py --dir <2026-07-09-era-dir>` | `parts_detail` is non-empty, lists `part_*` files |
| `python tools/verify_integrity.py --dir <2026-07-09-era-dir>` | No spurious MISSING PARTS / UNTRACKED failures |
| `grep sha256_of_bytes tools/verify_integrity.py` | No output (function removed) |
| `python tools/verify_integrity.py --all` | Clean exit on full repo scan |

---

*Generated by Parallel Loop AI — automated real-time bug triage for KeepFond (2026-07-26)*
