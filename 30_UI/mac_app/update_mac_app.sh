#!/bin/zsh
set -euo pipefail

running_sha=$1
installed_app=$2

# Fetch the official branch without changing the user's checkout.
git fetch --no-tags origin refs/heads/main
candidate=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
if [[ ! "$running_sha" =~ '^[0-9a-f]{7,40}$' ]] ||
   ! running_commit=$(git rev-parse --verify "${running_sha}^{commit}" 2>/dev/null); then
  print -u2 "Cannot verify the running build's commit: $running_sha"
  exit 11
fi
if [[ "$candidate" == "$running_commit" ]]; then
  exit 10
fi
if ! git merge-base --is-ancestor "$running_commit" "$candidate"; then
  print -u2 "origin/main does not contain the running build ($running_sha); skipped update."
  exit 11
fi

git log --oneline "${running_commit}..${candidate}"
stage=$(mktemp -d "${installed_app:h}/.webtpdf-update.XXXXXX")
backup="$stage/previous.app"
trap '
  if [[ -e "$backup" && ! -e "$installed_app" ]]; then
    if ! mv -- "$backup" "$installed_app"; then
      print -u2 "Could not restore the previous app from $backup"
      exit 1
    fi
  fi
  /bin/rm -rf -- "$stage"
' EXIT
git clone --quiet --shared --no-checkout "$(git rev-parse --show-toplevel)" "$stage/source"
git -C "$stage/source" checkout --quiet --detach "$candidate"
zsh "$stage/source/30_UI/mac_app/build_mac_app.sh"
built_app="$stage/source/40_Ops/apps/WebTPDF Mac.app"
codesign --verify --deep --strict "$built_app"

# The exit handler can restore the previous app if installing the build fails.
mv -- "$installed_app" "$backup"
mv -- "$built_app" "$installed_app"
