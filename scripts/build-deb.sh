#!/usr/bin/env bash
# Build the Debian package and the source tarball published beside it.
#
#   scripts/build-deb.sh [outdir]     -> outdir/helios_<version>_all.deb
#                                        outdir/helios_<version>.tar.gz
#
# Run from a checkout or from the public export tree (CI does the latter, so
# the package holds exactly the public surface). debian/changelog is generated
# from src/helios/__init__.py: the version has one home. GNU coreutils
# (`date -d`, `tar --sort`) — Linux only, which is where a .deb is built.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
out=$(realpath -m "${1:-$root/dist}")
version=$(python3 - "$root/src/helios/__init__.py" <<'PY'
import pathlib, re, sys
print(re.search(r'__version__ = "([^"]+)"', pathlib.Path(sys.argv[1]).read_text()).group(1))
PY
)
stamp=${SOURCE_DATE_EPOCH:-$(date +%s)}
export SOURCE_DATE_EPOCH="$stamp"
mkdir -p "$out"

# The source first, before the build writes anything into the tree. The
# screenshots are not source and would add megabytes to every archive release.
tar -C "$root/.." --sort=name --mtime="@$stamp" --owner=0 --group=0 --numeric-owner \
    --exclude=.git --exclude=dist --exclude=debian/changelog --exclude=docs/screenshots \
    --transform "s|^$(basename "$root")|helios-$version|" \
    -czf "$out/helios_$version.tar.gz" "$(basename "$root")"

cat > "$root/debian/changelog" <<CHANGELOG
helios ($version) resolute; urgency=medium

  * Release $version. See CHANGELOG.md.

 -- Norvi <apt@globalentry.systems>  $(date -u -d "@$stamp" '+%a, %d %b %Y %H:%M:%S +0000')
CHANGELOG
(cd "$root" && dpkg-buildpackage -us -uc -b)
mv "$root/../helios_${version}_all.deb" "$out/"
rm -f "$root"/../helios_"${version}"_*.buildinfo "$root"/../helios_"${version}"_*.changes
ls -l "$out"
