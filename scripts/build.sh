#!/usr/bin/env bash
# Build the Debian package from this tree.
#
#   scripts/build.sh [outdir]     -> outdir/helios_<version>_all.deb  (default: dist/)
#
# debian/changelog is generated from src/helios/__init__.py: the version has
# one home. Reproducible under SOURCE_DATE_EPOCH (default: the HEAD commit
# time). lintian runs when it is installed and fails the build on any error or
# warning. Needs the Build-Depends in debian/control; GNU coreutils (`date -d`),
# so Linux only, which is where a .deb is built.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=$(realpath -m "${1:-$root/dist}")
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$root/src/helios/__init__.py")
test -n "$version"
stamp=${SOURCE_DATE_EPOCH:-$(git -C "$root" log -1 --format=%ct 2>/dev/null || date +%s)}
export SOURCE_DATE_EPOCH="$stamp"
mkdir -p "$out"

cat > "$root/debian/changelog" <<CHANGELOG
helios ($version) resolute; urgency=medium

  * Release $version. See CHANGELOG.md.

 -- Norvi <apt@globalentry.systems>  $(date -u -d "@$stamp" '+%a, %d %b %Y %H:%M:%S +0000')
CHANGELOG
(cd "$root" && dpkg-buildpackage -us -uc -b)
deb="$out/helios_${version}_all.deb"
mv "$root/../helios_${version}_all.deb" "$deb"
rm -f "$root"/../helios_"${version}"_*.buildinfo "$root"/../helios_"${version}"_*.changes

# The version the package claims must be the tree's: a stale bump would publish
# a .deb apt never offers as an upgrade.
test "$(dpkg-deb -f "$deb" Version)" = "$version"
if command -v lintian >/dev/null; then
    lintian --fail-on error,warning "$deb"
else
    echo "lintian not installed; skipped" >&2
fi
ls -l "$out"
