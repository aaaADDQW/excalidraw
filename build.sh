#!/bin/bash
# ============================================================
# Build excalidraw_x86_64.deb for TOS 7
# ============================================================
# Usage:
#   ./build.sh                    # build from an existing webui/ dir
#   ./build.sh --with-frontend    # also build the web UI from upstream source
#
# Requirements:
#   - dpkg-deb (Debian/Ubuntu/WSL)
#   - python3 (reads config.ini)
#   - node 24 + yarn 1.22 (only for --with-frontend)
# ============================================================
set -e

APPID=$(python3 -c "import json;print(json.load(open('pkg/usr/local/excalidraw/config.ini'))['id'])")
VERSION=$(python3 -c "import json;print(json.load(open('pkg/usr/local/excalidraw/config.ini'))['version'])")
PLATFORM="x86_64"
APP_DIR="pkg/usr/local/${APPID}"
OUT_DIR="dist"
STAGING="build/staging"

echo "=== Building ${APPID} v${VERSION} for ${PLATFORM} ==="

# ------------------------------------------------------------
# Optional: build the web UI from upstream Excalidraw
# ------------------------------------------------------------
if [ "${1}" = "--with-frontend" ]; then
    echo "  Cloning upstream Excalidraw..."
    if [ ! -d excalidraw ]; then
        git clone --depth 1 https://github.com/excalidraw/excalidraw.git excalidraw
    fi

    echo "  Installing dependencies (this takes a few minutes)..."
    (cd excalidraw && yarn install --frozen-lockfile --ignore-scripts)

    echo "  Building web UI..."
    cp scripts/.env.production.local excalidraw/excalidraw-app/.env.production.local
    (cd excalidraw && yarn build:app:docker)

    echo "  Post-processing build output (TOS adaptations)..."
    python3 scripts/postprocess.py excalidraw/excalidraw-app/build

    echo "  Packing webui.bz2..."
    (cd excalidraw/excalidraw-app/build && tar -cjf "../../../${APP_DIR}/webui.bz2" .)
fi

# ------------------------------------------------------------
# Validate payload
# ------------------------------------------------------------
if [ ! -f "${APP_DIR}/webui.bz2" ]; then
    echo "ERROR: ${APP_DIR}/webui.bz2 is missing."
    echo "  Run './build.sh --with-frontend', or drop a pre-built webui.bz2 into ${APP_DIR}/."
    exit 1
fi
echo "  ✓ webui.bz2 found ($(du -h "${APP_DIR}/webui.bz2" | cut -f1))"

# §8.2 mandatory correspondences
python3 - <<'PY'
import json, os, sys

cfg = json.load(open("pkg/usr/local/excalidraw/config.ini"))
app_id = cfg["id"]
errs = []
if cfg["icon"] != f"/images/icons/{app_id}.svg":
    errs.append(f"icon must be /images/icons/{app_id}.svg")
if cfg["path"] != f"/{app_id}/":
    errs.append(f"path must be /{app_id}/")
if " " in cfg.get("publisher", ""):
    errs.append("publisher must not contain spaces (used as a Linux user name)")
if not os.path.isfile(f"pkg/usr/local/{app_id}/init.d/{cfg['system_id']}.service"):
    errs.append(f"missing init.d/{cfg['system_id']}.service")
if "type" in cfg and "open_path" in cfg:
    errs.append("'type' and 'open_path' are mutually exclusive")
if errs:
    print("  ✗ config.ini validation failed:")
    for e in errs:
        print("    -", e)
    sys.exit(1)
print("  ✓ config.ini validation passed")
PY

# ------------------------------------------------------------
# §4.6: force LF line endings on everything shipped to Linux
# ------------------------------------------------------------
find pkg -type f \( -name '*.sh' -o -name '*.py' -o -name '*.ini' -o -name '*.lang' \
    -o -name '*.service' -o -name '*.conf' -o -name '*.env' -o -name 'control' \
    -o -name 'postinst' -o -name 'prerm' -o -name 'postrm' -o -name 'preinst' \) \
    -exec sed -i 's/\r$//' {} +
echo "  ✓ CRLF -> LF normalized"

# ------------------------------------------------------------
# Build
# ------------------------------------------------------------
rm -rf build "${OUT_DIR}"
mkdir -p "${STAGING}/usr/local/${APPID}" "${OUT_DIR}"

cp -r pkg/DEBIAN "${STAGING}/DEBIAN"
cp -r "${APP_DIR}/." "${STAGING}/usr/local/${APPID}/"

chmod 0755 "${STAGING}/DEBIAN"/post* "${STAGING}/DEBIAN"/pre* 2>/dev/null || true
chmod 0755 "${STAGING}/usr/local/${APPID}/bin/"* 2>/dev/null || true

# --root-owner-group keeps the builder's uid/gid out of the package
dpkg-deb --build --root-owner-group "${STAGING}" "${OUT_DIR}/${APPID}_${PLATFORM}.deb"

echo ""
echo "=== Build complete ==="
echo "  Output: ${OUT_DIR}/${APPID}_${PLATFORM}.deb"
ls -lh "${OUT_DIR}/"
echo ""
echo "  MD5: $(md5sum "${OUT_DIR}/${APPID}_${PLATFORM}.deb" | cut -d' ' -f1)"
