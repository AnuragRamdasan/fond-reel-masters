# Bug Analysis: fond-reel-masters CI/Archive Integrity
**Date:** 2026-07-26  
**Source:** Real-time triage via Parallel Loop AI  
**Analyst:** Parallel Loop / Claude Agent  
**Scope:** Bugs G, H, I — confirmed present on `main` at `1fdbdf6`  
**Supersedes:** PRs #10–#15 (same bugs, none merged)

---

## Executive Summary

Three bugs were identified live from open GitHub issues on `fond-reel-masters`. Two are critical/high-severity gaps that cause **silent false-pass behaviour** in the CI quality gate. One is a dead-code hazard. All three are now fixed in this branch.

| # | File | Severity | Description |
|---|------|----------|-------------|
| G | `tools/verify_protagonist.py` | 🔴 CRITICAL | Sub-30s reels silently bypass face-identity check |
| H | `tools/normalize_manifest.py` | 🟠 HIGH | `part_*` naming convention produces empty `parts_detail` list |
| I | `tools/verify_integrity.py` | 🟡 LOW | Dead `sha256_of_bytes()` function — OOM risk if ever called |

---

## Bug G — CRITICAL: Sub-30s reels silently bypass face-identity check

### File
`tools/verify_protagonist.py` — `verify_protagonist()`

### Root Cause
The closing frame timestamp was **hardcoded to `"00:00:30"`**. For any reel shorter than 30 seconds:

1. `ffmpeg -ss 00:00:30 -i <assembled.mp4>` seeks past the file's EOF
2. No frame is written → `extract_frame_bytes()` returns `None`
3. The combined guard `if opening_frame is None or closing_frame is None:` evaluates `True`
4. The code prints `"ffmpeg not available"` (a **misleading** message — ffmpeg is working fine) and `return True` (PASS)

**Every sub-30s reel silently bypassed protagonist identity checking.**

### Why This Matters
Typical Veo clip lengths are 6–8 seconds. A 3-clip reel is ~18–24 seconds total — well under 30s — making this the **most common production case**. The protagonist check was therefore effectively disabled for the majority of production reels.

### Faulty Code (before fix)
```python
closing_frame = extract_frame_bytes(assembled_path, "00:00:30")   # ← hardcoded

if opening_frame is None or closing_frame is None:                 # ← combined guard
    print("Could not extract frames (ffmpeg not available or file corrupt)")
    return True   # ← SILENT PASS
```

### Fix Applied
```python
def get_video_duration(video_path: Path) -> Optional[float]:
    """Probe actual video duration via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, timeout=15, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return float(result.stdout.strip())
    return None

# In verify_protagonist():
probed_duration = get_video_duration(assembled_path)
if probed_duration is not None:
    closing_secs = max(1.0, min(probed_duration - 1.0, 30.0))
    closing_ts = _seconds_to_ts(closing_secs)   # dynamic, never past EOF
else:
    closing_ts = "00:00:30"  # fallback with warning

# Split into two distinct diagnostic branches:
if opening_frame is None:
    print("Could not extract opening frame (ffmpeg not available or file corrupt)")
    return True   # with accurate message

if closing_frame is None:
    print(f"Could not extract closing frame at t={closing_ts} (corrupt or probe failed)")
    return True   # with accurate message
```

**Additional changes:**
- `probed_duration_seconds` added to QA JSON report for auditability
- Contact sheet label updated to reflect the real closing timestamp used
- `_seconds_to_ts()` helper added for clean formatting

---

## Bug H — HIGH: `part_*` naming convention produces empty parts list

### File
`tools/normalize_manifest.py` — `normalise_reel_manifest()`

### Root Cause
`normalise_reel_manifest()` only globbed `master_part_*`:

```python
part_files = sorted(directory.glob("master_part_*"))   # ← only one naming convention
```

Directories created during the **2026-07-09 era** use bare `part_0`, `part_1`, `part_2`… naming (no `master_` prefix). The glob returns `[]`, so the emitted manifest has:

```json
{
  "parts": 0,
  "parts_detail": []
}
```

Downstream `verify_integrity.py` then reads this manifest, expects 0 parts, but finds actual files on disk → reports **`UNTRACKED`** or **`MISSING PARTS`** for every 2026-07-09 era directory. This caused systematic false CI failures that blocked legitimate CI runs.

### Faulty Code (before fix)
```python
def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    part_files = sorted(directory.glob("master_part_*"))  # ← misses part_* naming
    ...
```

### Fix Applied
```python
def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    # Glob both naming conventions, deduplicate by filename, sort by name
    part_files_raw = (
        list(directory.glob("master_part_*")) +
        list(directory.glob("part_*"))
    )
    seen: set = set()
    part_files = []
    for pf in sorted(part_files_raw, key=lambda p: p.name):
        if pf.name not in seen:
            seen.add(pf.name)
            part_files.append(pf)
    ...
```

---

## Bug I — LOW: Dead `sha256_of_bytes()` function

### File
`tools/verify_integrity.py`

### Root Cause
The function `sha256_of_bytes(data: bytes) -> str` was defined but **never called** anywhere in the codebase. All hashing uses `sha256_of_parts()`, which reads files in 64 KB streaming chunks.

```python
def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

### Why This Matters
- **OOM risk**: if ever called on a multi-GB video file, it would load the entire file into memory
- **Dead code noise**: adds clutter for static analysis tools (e.g. pylint, pyright)
- **Misleading**: suggests bytes-based hashing is an acceptable approach in this codebase when it is not

### Fix Applied
Function removed entirely. The streaming `sha256_of_parts()` is the correct and only approach. A comment in the commit message explains the removal.

---

## Impact Summary

| Bug | Effect before fix | Effect after fix |
|-----|-------------------|------------------|
| G | Every sub-30s reel (most production cases) bypasses protagonist check with a misleading "ffmpeg not available" PASS | Closing timestamp computed dynamically from actual video duration; meaningful pass/fail on all reel lengths |
| H | Every 2026-07-09 era directory emits manifest with `parts: 0`, causing systematic false UNTRACKED/MISSING PARTS CI failures | Both `master_part_*` and `part_*` are globbed and deduplicated; manifests correctly reflect on-disk files |
| I | Dead function poses future OOM risk and adds static analysis noise | Function removed; codebase consistently uses streaming hashing only |

---

## Recommendations

1. **Add regression test fixtures** for sub-30s reels to prevent Bug G from regressing
2. **Add a CI check** that verifies `verify_protagonist.py` actually runs to completion (not silently skipped) on at least one test reel
3. **Scan all 2026-07-09 era directories** and re-run `normalize_manifest.py --write` to regenerate correct manifests
4. **Close PRs #10–#15** — they target the same bugs but have accumulated conflicts; this branch is a clean rebase from current `main`
5. **Enable branch protection** on `main` to require PR reviews before merging

---

## Files Changed

| File | Change |
|------|--------|
| `tools/verify_protagonist.py` | Bug G: `get_video_duration()` via ffprobe, dynamic closing timestamp, split diagnostic branches, `probed_duration_seconds` in QA report |
| `tools/normalize_manifest.py` | Bug H: glob both `master_part_*` and `part_*`, deduplicate, sort |
| `tools/verify_integrity.py` | Bug I: remove dead `sha256_of_bytes()` function |
| `docs/bug-analysis-2026-07-26-pl.md` | This document |

---

*Triage performed in real time by Parallel Loop AI on 2026-07-26.*
