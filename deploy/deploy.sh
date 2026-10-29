#!/usr/bin/env bash
# Copy web/ to the server. Usage: deploy/deploy.sh user@host [/var/www/scan]
# Stamps the service worker with the git commit so phones fetch the new version.
set -euo pipefail
target="${1:?usage: deploy/deploy.sh user@host [remote_dir]}"
remote_dir="${2:-/var/www/scan}"
cd "$(dirname "$0")/.."

version="qrscan-$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
cp -R web/. "$build/"
sed -i.bak "s/^const VERSION = .*/const VERSION = \"$version\";/" "$build/sw.js" && rm "$build/sw.js.bak"

rsync -rlz --delete --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "$build/" "$target:$remote_dir/"
echo "deployed $version to $target:$remote_dir"
