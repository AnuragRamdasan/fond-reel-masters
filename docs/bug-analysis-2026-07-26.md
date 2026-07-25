# KeepFond — fond-reel-masters Bug Analysis
**Date:** 2026-07-26  
**Scope:** Live code audit of `tools/` and `.github/workflows/archive_integrity.yml`  
**Author:** Parallel Loop automated triage  
**Status:** All 4 bugs fixed in same commit batch

---

## Summary

Following the 13-bug fix cycle in PRs #1–#9 (documented in `docs/bug-analysis-2026-07-25.md`), a second live-code pass on 2026-07-26 identified **4 additional bugs** not covered by previous PRs.

| ID | File | Severity | Description |
|----|------|----------|-------------|
| Bug C | `tools/verify_protagonist.py` | **CRITICAL** | `qa_dir` path collision — multiple `masters/YYYY-MM-DD-*` dirs share the same report path |
| Bug D | `tools/normalize_manifest.py` | **HIGH** | `scan_all()` passes file paths (not dirs) to `process_directory()`, causing `NotADirectoryError` |
| Bug E | `.github/workflows/archive_integrity.yml` | **HIGH** | Unguarded `grep` in "Detect changed directories" step aborts CI when no files match |
| Bug F | `tools/verify_protagonist.py` | **MEDIUM** | `draw.rectangle()` fill alpha ignored on RGB image — contact sheet overlay is solid black not semi-transparent |

---

## Bug C — CRITICAL: `qa_dir` collision in `verify_protagonist.py`

### Root Cause
In `verify_protagonist.py::main()`, when `--qa-dir` is not provided, the fallback computes:

```python
m = re.search(r"\d{4}-\d{2}-\d{2}", target_dir.name)
date = m.group(0) if m else target_dir.name
qa_dir = repo_root / "qa" / date
```

`target_dir.name` is the **last path component** of `--dir`. For:
- `--dir masters/2026-07-24-final` → `target_dir.name = "2026-07-24-final"` → `date = "2026-07-24"`
- `--dir masters/2026-07-24-draft` → `target_dir.name = "2026-07-24-draft"` → `date = "2026-07-24"`

Both resolve to `qa/2026-07-24/`, so the second run **silently overwrites** the first's `protagonist_check.json` and `diag_faces.jpg`.

When CI checks multiple same-date master variants (a common pattern: `final`, `draft`, `v2`), only the last run's report survives. This can mask a failing check.

### Fix
Use the full path relative to repo root, mirroring the fix applied to `verify_integrity.py` (Bug #2 in PR #3):

```python
# Before (broken)
m = re.search(r"\d{4}-\d{2}-\d{2}", target_dir.name)
date = m.group(0) if m else target_dir.name
qa_dir = repo_root / "qa" / date

# After (fixed)
try:
    rel = target_dir.relative_to(repo_root)
except ValueError:
    rel = Path(target_dir.name)
qa_dir = repo_root / "qa" / str(rel).replace("/", "-")
```

---

## Bug D — HIGH: `scan_all()` passes file paths to `process_directory()` in `normalize_manifest.py`

### Root Cause
In `normalize_manifest.py::scan_all()`:

```python
for parent in ["ads-bridge", "masters"]:
    p = root / parent
    if p.exists():
        for sub in p.iterdir():    # <-- iterdir() yields both files AND dirs
            if sub.is_dir():       # <-- MISSING: this check was absent
                dirs.append(sub)
```

Wait — checking the current live code more carefully: the live code does **not** have `if sub.is_dir()` guard in the `masters`/`ads-bridge` loop. The loop is:

```python
for parent in ["ads-bridge", "masters"]:
    p = root / parent
    if p.exists():
        for sub in p.iterdir():
            dirs.append(sub)   # appends ALL entries, including files
```

`p.iterdir()` yields **all** entries — files and directories. When a file (e.g. `masters/README.md`) is appended to `dirs` and passed to `process_directory()`:

1. `detect_schema(directory)` calls `directory / "manifest.json"` which becomes `masters/README.md/manifest.json` — a path that never exists
2. Falls through to `create_empty_manifest(directory)`
3. `directory.rglob("*")` on a **file path** raises `NotADirectoryError` in Python 3.11+

This silently crashes the normaliser for any `masters/` or `ads-bridge/` parent that contains loose files (READMEs, `.gitignore`, etc.).

### Fix
Add `if sub.is_dir():` guard:

```python
for parent in ["ads-bridge", "masters"]:
    p = root / parent
    if p.exists():
        for sub in p.iterdir():
            if sub.is_dir():       # FIX: skip files, only process directories
                dirs.append(sub)
```

---

## Bug E — HIGH: Unguarded `grep` in CI "Detect changed directories" step

### Root Cause
In `.github/workflows/archive_integrity.yml`, the `Detect changed directories` step:

```bash
git diff --name-only HEAD~1 HEAD | \
  grep -E '^(masters/[^/]+|ads-bridge/[^/]+)/' | \
  sed 's|\([^/]*/[^/]*\)/.*|\1|' | sort -u > /tmp/changed_dirs.txt

git diff --name-only HEAD~1 HEAD | \
  grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}/' | \
  cut -d/ -f1 | sort -u >> /tmp/changed_dirs.txt
```

GitHub Actions runs shell steps with `set -eo pipefail` by default. `grep` exits with code **1** when it finds **no matches**. If a commit only touches top-level files (e.g. `README.md`, `requirements.txt`), the first `grep` finds no nested-dir changes and exits 1 — which, under `pipefail`, propagates and **aborts the entire step**.

Result: `GITHUB_OUTPUT` never gets `scan_all=false` written, subsequent steps that read `steps.changed-dirs.outputs.scan_all` get an empty string, and the integrity check runs in an undefined state (the `[[ "" == "true" ]]` comparison evaluates to false, but `/tmp/changed_dirs.txt` was never created, so the `while IFS= read -r dir` loop errors with `No such file or directory`).

### Fix
Add `|| true` to each `grep` call to prevent non-zero exit on empty match:

```bash
git diff --name-only HEAD~1 HEAD | \
  grep -E '^(masters/[^/]+|ads-bridge/[^/]+)/' || true | \
  sed ...

# Better: use process substitution to isolate grep exit code
{ git diff --name-only HEAD~1 HEAD | grep -E '^(masters/[^/]+|ads-bridge/[^/]+)/' || true; } | \
  sed 's|...' | sort -u > /tmp/changed_dirs.txt
```

Also ensure the file always exists:
```bash
touch /tmp/changed_dirs.txt
```

---

## Bug F — MEDIUM: RGB image + alpha fill → solid black overlay in contact sheet

### Root Cause
In `verify_protagonist.py::make_contact_sheet()`:

```python
img = Image.open(io.BytesIO(data)).convert("RGB")   # RGB mode
...
draw.rectangle([(0, 0), (320, 30)], fill=(0, 0, 0, 180))  # 4-tuple: RGBA
```

PIL's `ImageDraw.rectangle()` with an RGBA fill tuple on an **RGB** image silently truncates the alpha channel and treats it as `fill=(0, 0, 0)` — a **solid black** rectangle. The intended semi-transparent overlay (alpha=180/255 ≈ 70% opaque) never appears; instead the label background is fully opaque black, obscuring frame content near the top of each thumbnail.

This doesn't crash, but produces diagnostic contact sheets where the label blocks content, making face-drift diagnosis harder.

### Fix
Composite via RGBA then convert back to RGB:

```python
img = Image.open(io.BytesIO(data)).convert("RGBA")
overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
draw_overlay = ImageDraw.Draw(overlay)
draw_overlay.rectangle([(0, 0), (320, 30)], fill=(0, 0, 0, 180))
img = Image.alpha_composite(img, overlay).convert("RGB")
draw = ImageDraw.Draw(img)
draw.text((8, 6), label, fill=(255, 255, 255))
```

---

## Impact Assessment

| Bug | When triggered | Impact |
|-----|---------------|--------|
| C | CI checks 2+ `masters/YYYY-MM-DD-*` dirs in one run | Protagonist reports silently overwritten; failing reel may appear to pass if last dir wins |
| D | `normalize_manifest.py --all` on any repo with loose files in `masters/` or `ads-bridge/` | `NotADirectoryError` crash; normaliser exits early, leaving manifests un-migrated |
| E | Push/PR that only changes root-level files | CI step aborts; changed_dirs output missing; subsequent integrity job has undefined behavior |
| F | Contact sheet generation with PIL | Diagnostic images have solid-black label bar instead of semi-transparent; harder to review |

---

## Follow-on Recommendations

1. **Regression fixtures:** Add a CI test that pushes a root-only file change (e.g. `requirements.txt` bump) and verifies the `Detect changed directories` step completes without error.
2. **`qa_dir` uniqueness:** Adopt the `str(rel).replace("/", "-")` pattern consistently in both `verify_integrity.py` and `verify_protagonist.py` — already done in this fix batch.
3. **File-guard pattern:** All `iterdir()` loops that expect directories should always include `if sub.is_dir()`. Add a lint rule or comment to enforce this.
4. **RGBA contact sheets:** Consider storing contact sheet thumbnails at higher quality (JPEG q=90) and with resolution 480×720 for better face-drift visibility.
