# Bug Analysis: CI Layer Bugs #9–#12
**Date**: 2026-07-26 | **Repo**: AnuragRamdasan/fond-reel-masters | **4 new bugs fixed in CI workflow**

---

## Context

After fixing Bugs #1–#8 (tool-level Python bugs) on 2026-07-25, the `archive_integrity.yml` CI workflow continued to fail on every single run — **31 consecutive failures**. The root causes were all in the CI workflow layer itself, not in the Python tools.

---

## Bug #9 — CRITICAL: Integrity check runs before manifest auto-generation (step ordering inversion)

### Root Cause
The 2026-07-25 fixes correctly made `manifest_missing` count as a failure in `verify_integrity.py` (Bug #1 fix). However, the CI workflow ran the integrity check **before** auto-generating missing manifests:

```
OLD ORDER:
  1. Run integrity check       ← detects missing manifest → exit_code=1
  2. Check for missing manifests
  3. Auto-generate manifests   ← correctly creates them (too late)
  4. Fail workflow             ← fires on exit_code=1 from step 1
```

**Result**: Any run touching a directory without a manifest would:
- Detect the missing manifest → `exit_code=1`
- Later auto-generate the manifest correctly
- Fail the workflow anyway, because the failure gate read `exit_code` from the **pre-auto-generation** integrity check

This caused **every single push** to fail even when the only real issue was a missing manifest stub (which was immediately and correctly created).

### Fix
Reorder the steps so manifest auto-generation runs first:

```
NEW ORDER:
  1. Check for missing manifests
  2. Auto-generate manifests   ← creates stubs before integrity check
  3. Commit auto-generated manifests
  4. Run integrity check       ← now sees the generated manifests → passes
  5. Run protagonist check
  6. Fail workflow (only if real failures remain)
```

---

## Bug #10 — HIGH: Issue creation crashes workflow (issues disabled on repo)

### Root Cause
The "Open issue on failure" step calls `github.rest.issues.create()`. The `fond-reel-masters` repo has **issues disabled** (`has_issues: false`). GitHub returns a **410 Gone** response, which throws an unhandled exception in the `actions/github-script` runner.

Because the step had no `continue-on-error: true`, this crash **killed the entire workflow** before the final `Fail workflow` steps could run — swallowing the actual error message and making it impossible to see what really failed.

Additionally, the `listForRepo` call for existing issues also returns 410 for disabled-issues repos, so the dedup check also threw.

### Fix
1. Add `continue-on-error: true` to the "Open issue on failure" step
2. Wrap both `listForRepo` and `create` in a `try/catch` that logs a warning when issues are disabled, rather than throwing

---

## Bug #11 — MEDIUM: Bare date directories never checked for missing manifests

### Root Cause
The missing-manifest check used the glob:
```bash
for dir in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*/; do
```

The trailing `-*` requires a **suffix after the date** (e.g. `2026-07-09-remaster/`). Plain date directories like `2026-07-09/` never match this glob.

**Impact**: All bare date archive directories (which are the majority of the archive) were silently skipped during the missing-manifest check — they could have no manifest at all and CI would never detect it or auto-generate one.

### Fix
Add a second glob for bare date directories:
```bash
# Suffixed date dirs  (e.g. 2026-07-09-remaster/)
for dir in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-*/; do ...
# Bare date dirs      (e.g. 2026-07-09/)
for dir in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/; do ...
```

---

## Bug #12 — MEDIUM: ads-bridge/ subdirectories never checked for missing manifests

### Root Cause
The missing-manifest check only covered `masters/*/` and date-named directories. The `ads-bridge/*/` tree was completely omitted — ads-bridge archives could have no manifest and CI would never detect or auto-generate one.

### Fix
Add a dedicated check block for `ads-bridge/*/`:
```bash
for dir in ads-bridge/*/; do
  if [ -d "$dir" ] && [ ! -f "${dir}manifest.json" ]; then
    MISSING="$MISSING\n- $dir"
  fi
done
```

---

## Impact Summary

| Bug | Severity | Effect Before Fix |
|-----|----------|-------------------|
| #9 — Step ordering inversion | CRITICAL | 100% of CI runs fail even on clean pushes with missing-manifest-only issues |
| #10 — Issues API crash | HIGH | Workflow dies silently before emitting the real error; actual failure reason swallowed |
| #11 — Bare date dir glob miss | MEDIUM | All plain date archives skip manifest check; missing manifests go undetected indefinitely |
| #12 — ads-bridge omitted | MEDIUM | All ads-bridge archives skip manifest check; missing manifests go undetected indefinitely |

---

## Files Changed

- `.github/workflows/archive_integrity.yml` — step reorder, `continue-on-error`, glob fixes, try/catch in JS

---

## Verification

After this fix, a push that triggers the workflow on a directory with no manifest should:
1. Detect missing manifest
2. Auto-generate it (`normalize_manifest.py --all --write`)
3. Commit and push the manifest
4. Run integrity check → sees generated manifest → ✅ passes (unverified SHA, but not a hard failure)
5. Run protagonist check
6. Workflow completes green

A push with a genuine SHA256 mismatch or part-count error should still fail at step 4 (after auto-gen), surfacing the real error.
