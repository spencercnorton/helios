#!/usr/bin/env bash
# Install Helios .desktop file + icon into the user's local applications.
#
# The source icon (data/icons/dev.norvi.Helios.png) is a 1254x1254 master
# that we resize via Pillow to every standard icon-theme size. This means
# GNOME / KDE / Xfce all pick the right resolution for the dock, app grid,
# and HiDPI displays. We deliberately ship PNG-only (no SVG) because the
# icon is a photographic-style image, not a vector mark.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELIOS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_ID="dev.norvi.Helios"
APP_DIR="${HOME}/.local/share/applications"
THEME_ROOT="${HOME}/.local/share/icons/hicolor"
SOURCE_ICON="${HELIOS_ROOT}/data/icons/${APP_ID}.png"

mkdir -p "${APP_DIR}"

# Render desktop file with absolute path to launcher.
DESKTOP_FILE="${APP_DIR}/${APP_ID}.desktop"
sed "s|@LAUNCHER@|${HELIOS_ROOT}/scripts/helios|g" \
    "${HELIOS_ROOT}/data/${APP_ID}.desktop.in" > "${DESKTOP_FILE}"
chmod +x "${DESKTOP_FILE}"

# Generate every standard icon-theme size from the master PNG.
python3 - <<PY
from PIL import Image
from pathlib import Path

SRC = Path("${SOURCE_ICON}")
APP_ID = "${APP_ID}"
THEME_ROOT = Path("${THEME_ROOT}")
SIZES = [16, 24, 32, 48, 64, 128, 256, 512]

img = Image.open(SRC).convert("RGBA")
for sz in SIZES:
    out = THEME_ROOT / f"{sz}x{sz}" / "apps"
    out.mkdir(parents=True, exist_ok=True)
    img.resize((sz, sz), Image.LANCZOS).save(
        out / f"{APP_ID}.png", "PNG", optimize=True
    )
PY

# Remove any stale SVG from a prior install — we ship PNG-only now.
rm -f "${THEME_ROOT}/scalable/apps/${APP_ID}.svg"

# Refresh caches.
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${THEME_ROOT}" 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APP_DIR}" 2>/dev/null || true
fi

echo "Installed Helios:"
echo "  Desktop entry: ${DESKTOP_FILE}"
echo "  Icons:         ${THEME_ROOT}/{16,24,32,48,64,128,256,512}x.../apps/${APP_ID}.png"
echo "  Launcher:      ${HELIOS_ROOT}/scripts/helios"
echo
echo "Helios should now appear in your app grid. Log out and back in if it doesn't."
