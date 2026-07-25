# Bug Analysis — fond-reel-masters CI/Archive Integrity Audit (2026-07-26, Session 2)

**Date:** 2026-07-26  
**Analyst:** Parallel Loop AI (automated real-time triage)  
**Scope:** `tools/verify_protagonist.py`, `tools/normalize_manifest.py`, `tools/verify_integrity.py`  
**Prior sessions:** `docs/bug-analysis-2026-07-25.md`, `docs/bug-analysis-2026-07-26.md`  
**PR:** `fix/bug-g-h-i-2026-07-26`

---

## Executive Summary

Three additional bugs were identified through static analysis of the current `main` branch source after the fixes from Sessions 1 (2026-07-25) and 2 (2026-07-26) were merged.

| ID | Severity | File | Status |
|----|----------|------|--------|
| G  | **CRITICAL** | `verify_protagonist.py` | Fixed in this PR |
| H  | **HIGH**     | `normalize_manifest.py`  | Fixed in this PR |
| I  | LOW          | `verify_integrity.py`    | Fixed in this PR |

---

## Bug G — Short-reel closing frame causes silent PASS (CRITICAL)

### Affected file
`tools/verify_protagonist.py` — `verify_protagonist()`

### Root cause
The closing frame timestamp was hardcoded to `"00:00:30"`:

```python
print("  🎞️   Extracting closing frame (t=00:00:30)…")
closing_frame = extract_frame_bytes(assembled_path, "00:00:30")
```

For any reel whose assembled duration is **shorter than 30 seconds**, `ffmpeg` cannot seek past EOF. It exits with a non-zero return code, and `extract_frame_bytes()` returns `None`.

The caller's None-check is:

```python
if opening_frame is None or closing_frame is None:
    print("  ⚠️   Could not extract frames (ffmpeg not available or file corrupt)")
    print("  ✅  Check skipped (no ffmpeg) — treating as PASS")
    return True
```

This means **every sub-30-second reel silently passes the protagonist check** with the misleading message "no ffmpeg" — even when `ffmpeg` is fully installed and operational. An adversarially or accidentally swapped protagonist in a short reel would never be caught.

### Impact
- Any short reel (< 30 s) — including test clips, trailers, or ads — is completely exempt from the protagonist identity check, defeating its purpose.
- CI gives a false ✅ with a misleading diagnostic message.

### Fix
1. Added `get_video_duration(video_path)` which uses `ffprobe` to return the assembled file's duration in seconds.
2. Compute `closing_ts = max(1s, min(duration − 1s, 30s))` so the closing timestamp is always safely within the video.
3. Separated the `opening_frame is None` and `closing_frame is None` checks into distinct diagnostic branches with clear explanations (ffmpeg missing vs. very-short reel).
4. Stored `probed_duration_seconds` in the QA JSON report.

```python
# NEW: probe actual duration
duration = get_video_duration(assembled_path)
if duration is not None:
    safe_close = max(1.0, min(duration - 1.0, 30.0))
    closing_ts = f"{int(safe_close // 3600):02d}:{int((safe_close % 3600) // 60):02d}:{safe_close % 60:05.2f}"
else:
    closing_ts = "00:00:30"  # fallback with warning
```

---

## Bug H — `normalise_reel_manifest()` misses `part_*` files (HIGH)

### Affected file
`tools/normalize_manifest.py` — `normalise_reel_manifest()`

### Root cause
```python
def normalise_reel_manifest(raw: Dict, directory: Path) -> Dict:
    part_files = sorted(directory.glob("master_part_*"))  # ← only master_part_*
```

The function only globbed `master_part_*` to populate `parts_detail`. However, the repo uses **two naming conventions** for video part files:
- `master_part_0`, `master_part_1`, … (used by most directories)
- `part_0`, `part_1`, … (used by early 2026-07-09 dates and some audition directories)

For any directory using the `part_*` convention, `parts_detail` is empty and the emitted schema-2 manifest reports `"parts": 0` even when video files exist. This causes:
- `verify_integrity.py` to see 0 expected parts, producing spurious `MISSING PARTS` or `UNTRACKED` errors on the next run.
- The normalizer appearing to succeed (no error is raised), silently corrupting the migrated manifest.

### Impact
- All `part_*`-named archives get broken manifests on migration, causing downstream verification failures.
- Silent corruption: no error or warning is emitted during the migration step.

### Fix
```python
# FIX Bug H: collect both naming conventions and deduplicate by name.
seen_names: Dict[str, Path] = {}
for pf in list(directory.glob("master_part_*")) + list(directory.glob("part_*")):
    seen_names[pf.name] = pf
part_files = sorted(seen_names.values(), key=lambda p: p.name)
```

Deduplication by filename is necessary in case a future directory contains both patterns (e.g., during a rename in progress).

---

## Bug I — Dead function `sha256_of_bytes()` in `verify_integrity.py` (LOW)

### Affected file
`tools/verify_integrity.py`

### Root cause
```python
def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

This function is **defined but never called** anywhere in the file. All actual hashing uses `sha256_of_parts()`. The dead function:
- Increases the maintenance surface (a future developer might mistakenly use it for file hashing, not realising it requires the entire file to be buffered in memory).
- Could cause confusion when reading the code: two hash functions with similar names suggest both are used.
- Was explicitly called out in earlier commit messages (`remove dead sha256_of_file`) but the analogous `sha256_of_bytes` was left behind.

### Impact
- No runtime impact.
- Maintenance and readability risk.

### Fix
Removed the function. All call sites already use `sha256_of_parts()`.

---

## Recommendations

1. **Regression fixture for short reels (Bug G):** Add a 5-second synthetic MP4 to `qa/fixtures/` and assert that `verify_protagonist.py` runs the face check (not skips it) against it.
2. **Naming convention test (Bug H):** Add a unit test in `qa/` that creates a temp directory with `part_0` files, runs `normalise_reel_manifest()`, and asserts `parts_detail` is non-empty.
3. **Branch protection:** Require CI to pass on `main` to prevent regressions slipping through via direct pushes.
4. **ffprobe availability check in CI:** The `archive_integrity.yml` already installs `ffmpeg`; verify that `ffprobe` is also on PATH (it ships with ffmpeg packages, but explicit verification prevents subtle omissions).
