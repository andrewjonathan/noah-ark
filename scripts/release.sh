#!/usr/bin/env bash
# Release noah-ark to PyPI from your machine (GitHub Actions is paused).
#
# Usage:
#   scripts/release.sh <version | patch | minor | major> [--dry-run] [--yes]
#
# Examples:
#   scripts/release.sh patch            # 0.1.0 -> 0.1.1
#   scripts/release.sh 0.2.0
#   scripts/release.sh minor --dry-run  # everything except upload, commit, tag, push
#
# Steps: bump version -> ruff + pytest -> build + twine check -> upload to PyPI
#        -> commit + tag -> push.
#
# PyPI credentials: set PYPI_TOKEN (a project-scoped API token, "pypi-..."),
# or configure ~/.pypirc. Environment overrides: PYTHON (interpreter to use),
# REMOTE (git remote, default origin).

set -euo pipefail

INIT_FILE="src/noah_ark/__init__.py"
REMOTE="${REMOTE:-origin}"
BRANCH="main"

die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
confirm() {
  [[ "$ASSUME_YES" == 1 ]] && return 0
  read -r -p "$1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}
usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# --- arguments ---------------------------------------------------------------
TARGET=""
DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -y|--yes)  ASSUME_YES=1 ;;
    -h|--help) usage 0 ;;
    -*)        die "unknown option: $arg" ;;
    *)         [[ -z "$TARGET" ]] || die "only one version argument allowed"; TARGET="$arg" ;;
  esac
done
[[ -n "$TARGET" ]] || usage 1

cd "$(git rev-parse --show-toplevel)"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
fi

# --- pre-flight checks -------------------------------------------------------
step "Pre-flight checks"
current_branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$current_branch" == "$BRANCH" ]] || die "must be on '$BRANCH' (currently on '$current_branch')"
[[ -z "$(git status --porcelain)" ]] || die "working tree is not clean; commit or stash first"

CURRENT="$(sed -nE 's/^__version__ = "([^"]+)"$/\1/p' "$INIT_FILE")"
[[ -n "$CURRENT" ]] || die "could not read __version__ from $INIT_FILE"

if [[ "$TARGET" =~ ^(patch|minor|major)$ ]]; then
  IFS=. read -r MA MI PA <<<"${CURRENT%%[!0-9.]*}"
  case "$TARGET" in
    patch) NEW="$MA.$MI.$((PA + 1))" ;;
    minor) NEW="$MA.$((MI + 1)).0" ;;
    major) NEW="$((MA + 1)).0.0" ;;
  esac
else
  NEW="${TARGET#v}"
fi
[[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+([abc]|rc)?[0-9]*$ ]] || die "invalid version: $NEW"
[[ "$NEW" != "$CURRENT" ]] || die "version $NEW is already the current version"
TAG="v$NEW"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"

"$PYTHON" -c "import build, twine, ruff, pytest" 2>/dev/null \
  || die "missing tools; run: $PYTHON -m pip install -e '.[dev]' twine"

if [[ "$DRY_RUN" == 0 && -z "${PYPI_TOKEN:-}" && ! -f "$HOME/.pypirc" ]]; then
  die "no PyPI credentials: export PYPI_TOKEN=pypi-... or create ~/.pypirc"
fi

echo "Releasing noah-ark $CURRENT -> $NEW (tag $TAG)$([[ $DRY_RUN == 1 ]] && echo ' [dry run]')"

# Restore the version file if anything fails before the release commit.
COMMITTED=0
cleanup() {
  if [[ "$COMMITTED" == 0 ]]; then
    git checkout -- "$INIT_FILE" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# --- 1. bump version ---------------------------------------------------------
step "Bumping version in $INIT_FILE"
sed -i.bak -E "s/^__version__ = \"[^\"]+\"$/__version__ = \"$NEW\"/" "$INIT_FILE" && rm -f "$INIT_FILE.bak"
grep -q "^__version__ = \"$NEW\"$" "$INIT_FILE" || die "version bump failed"

# --- 2. lint and test --------------------------------------------------------
step "Running ruff"
"$PYTHON" -m ruff check .
step "Running pytest"
"$PYTHON" -m pytest -q

# --- 3. build and check ------------------------------------------------------
step "Building sdist and wheel"
rm -rf dist
"$PYTHON" -m build
step "Checking distributions with twine"
"$PYTHON" -m twine check --strict dist/*
ls -1 dist/

if [[ "$DRY_RUN" == 1 ]]; then
  step "Dry run finished; nothing uploaded, committed, tagged or pushed"
  exit 0
fi

# --- 4. upload to PyPI -------------------------------------------------------
step "Uploading to PyPI"
confirm "Upload noah-ark $NEW to PyPI? This cannot be undone." || die "aborted before upload"
if [[ -n "${PYPI_TOKEN:-}" ]]; then
  TWINE_USERNAME=__token__ TWINE_PASSWORD="$PYPI_TOKEN" "$PYTHON" -m twine upload --non-interactive dist/*
else
  "$PYTHON" -m twine upload dist/*
fi

# --- 5. commit, tag and push -------------------------------------------------
step "Committing and tagging $TAG"
git add "$INIT_FILE"
git commit -q -m "Release $TAG"
git tag -a "$TAG" -m "noah-ark $NEW"
COMMITTED=1

step "Pushing to $REMOTE"
if confirm "Push $BRANCH and tag $TAG to $REMOTE?"; then
  git push "$REMOTE" "$BRANCH"
  git push "$REMOTE" "$TAG"
else
  echo "Skipped push. Run later: git push $REMOTE $BRANCH && git push $REMOTE $TAG"
fi

step "Released noah-ark $NEW: https://pypi.org/project/noah-ark/$NEW/"
