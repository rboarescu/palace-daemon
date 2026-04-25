#!/usr/bin/env bash
# apply_patches.sh — re-apply local patches to the mempalace pipx install
# Run this after every: pipx upgrade mempalace
#
# Usage:
#   bash scripts/apply_patches.sh
#   bash scripts/apply_patches.sh --check   # dry-run, no changes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCHES_DIR="$SCRIPT_DIR/../patches"
VENV_SITE="$(/home/radu/.local/share/pipx/venvs/mempalace/bin/python \
    -c 'import site; print(site.getsitepackages()[0])')"

DRY_RUN=0
[[ "${1:-}" == "--check" ]] && DRY_RUN=1

MEMPALACE_VERSION="$(/home/radu/.local/share/pipx/venvs/mempalace/bin/python \
    -c 'import mempalace; print(mempalace.__version__)' 2>/dev/null || echo unknown)"

echo "mempalace version : $MEMPALACE_VERSION"
echo "site-packages     : $VENV_SITE"
echo "patches dir       : $PATCHES_DIR"
[[ $DRY_RUN -eq 1 ]] && echo "(dry-run — no changes will be made)"
echo ""

APPLIED=0
SKIPPED=0
FAILED=0

for patch in "$PATCHES_DIR"/*.patch; do
    [[ -f "$patch" ]] || continue
    name="$(basename "$patch")"

    # Check if already applied
    if patch --dry-run -p1 -R --quiet -d "$VENV_SITE" < "$patch" 2>/dev/null; then
        echo "  [already applied] $name"
        ((SKIPPED++)) || true
        continue
    fi

    # Check if applicable
    if ! patch --dry-run -p1 --quiet -d "$VENV_SITE" < "$patch" 2>/dev/null; then
        echo "  [CONFLICT]        $name  <-- upstream may have changed this code; review manually"
        ((FAILED++)) || true
        continue
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [would apply]     $name"
        ((APPLIED++)) || true
    else
        patch -p1 -d "$VENV_SITE" < "$patch"
        echo "  [applied]         $name"
        ((APPLIED++)) || true
    fi
done

echo ""
echo "Results: $APPLIED applied, $SKIPPED already-applied, $FAILED conflicts"

if [[ $FAILED -gt 0 ]]; then
    echo ""
    echo "Action required: $FAILED patch(es) conflicted."
    echo "Check if upstream fixed the issue — if so, remove the patch file."
    echo "Otherwise update the patch to match the new upstream code."
    exit 1
fi

if [[ $DRY_RUN -eq 0 && $APPLIED -gt 0 ]]; then
    echo ""
    echo "Restart the daemon to pick up changes:"
    echo "  sudo systemctl restart palace-daemon"
fi
