#!/usr/bin/env bash
# Cut a release: bump the version, tag, push, and draft a GitHub Release.
#
#   scripts/release.sh patch            # 0.1.0 -> 0.1.1
#   scripts/release.sh minor            # 0.1.0 -> 0.2.0
#   scripts/release.sh major            # 0.1.0 -> 1.0.0
#   scripts/release.sh 0.3.0            # an explicit version
#   scripts/release.sh patch --dry-run  # show what would happen, change nothing
#   scripts/release.sh patch --final    # not marked as a pre-release
#
# Nothing reaches PyPI until you press "Publish release" on the draft: that
# click triggers .github/workflows/publishing.yml.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

VERSION_FILE=src/prongs/__init__.py
BRANCH=master

bump="" dry=0 prerelease=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    --final) prerelease=0 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) [ -z "$bump" ] || { echo "one version argument, got: $bump and $arg" >&2; exit 2; }
       bump="$arg" ;;
  esac
done
[ -n "$bump" ] || { echo "usage: scripts/release.sh patch|minor|major|X.Y.Z [--dry-run] [--final]" >&2; exit 2; }

die() { echo "error: $*" >&2; exit 1; }
run() { if [ "$dry" = 1 ]; then echo "  would run: $*"; else "$@"; fi; }

current=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$VERSION_FILE")
[[ "$current" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || die "cannot read a X.Y.Z version from $VERSION_FILE"
major=${BASH_REMATCH[1]} minor=${BASH_REMATCH[2]} patch=${BASH_REMATCH[3]}
case "$bump" in
  patch) new="$major.$minor.$((patch + 1))" ;;
  minor) new="$major.$((minor + 1)).0" ;;
  major) new="$((major + 1)).0.0" ;;
  *) [[ "$bump" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "not a version or patch|minor|major: $bump"
     new="$bump" ;;
esac
tag="v$new"
echo "release: $current -> $new ($tag)"

# --- the tree this release is cut from
command -v gh >/dev/null || die "the GitHub CLI (gh) is not installed"
[ "$(git branch --show-current)" = "$BRANCH" ] || die "not on $BRANCH"
[ -z "$(git status --porcelain)" ] || die "the working tree has uncommitted changes"
git fetch -q origin "$BRANCH" --tags
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] || die "$BRANCH and origin/$BRANCH differ: pull or push first"
[ "$new" != "$current" ] || die "$new is already the version"
[ "$(printf '%s\n%s\n' "$current" "$new" | sort -V | tail -1)" = "$new" ] || die "$new is lower than $current"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
# PyPI never accepts the same version twice
name=$(sed -n 's/^name = "\(.*\)"$/\1/p' pyproject.toml)
code=$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$name/$new/json" || true)
[ "$code" != 200 ] || die "$name $new is already on PyPI"

# --- the tests, on the code as it will ship
py=python3; [ -x .venv/bin/python ] && py=.venv/bin/python
echo "running the tests ($py)"
"$py" -m pytest -q

# --- bump, tag, push, draft
run sed -i "s/^__version__ = \"$current\"$/__version__ = \"$new\"/" "$VERSION_FILE"
run git commit -q -am "release: $new"
run git tag "$tag"
run git push -q origin "$BRANCH" "$tag"
flags=(--draft --generate-notes --title "$name $new")
[ "$prerelease" = 1 ] && flags+=(--prerelease)
run gh release create "$tag" "${flags[@]}"

if [ "$dry" = 1 ]; then
  echo "dry run: nothing was changed"
else
  repo=$(gh repo view --json url --jq .url)
  echo "drafted $tag. Publish it to upload to PyPI: $repo/releases"
fi
