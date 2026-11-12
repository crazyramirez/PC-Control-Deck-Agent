#!/usr/bin/env bash
# Builds the PC app on Linux or macOS with PyInstaller (Windows: scripts/build-agent.ps1).
#   bash scripts/build-agent.sh
#     Linux -> dist/release/PCControlDeck-<ver>-linux-<arch>.tar.gz  (app + .desktop + icon + install.sh)
#     macOS -> dist/release/PCControlDeck-<ver>-macos-<arch>.zip and .dmg  (unsigned "PC Control Deck.app")
# Environment: PYTHON (default .venv/bin/python), SKIP_TESTS=1 to skip pytest.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "run scripts/setup-agent.sh first (or set PYTHON)" >&2
    exit 1
fi
export PYTHONPATH=src
VERSION=$("$PY" -c "import pcdeck_agent; print(pcdeck_agent.__version__)")
ARCH=$(uname -m)
echo "== PC Control Deck $VERSION ($(uname -s) $ARCH)"

if [ "${SKIP_TESTS:-0}" != "1" ]; then
    QT_QPA_PLATFORM=offscreen "$PY" -m pytest -q
fi

mkdir -p build dist/release
"$PY" packaging/make_icon.py build/pcdeck.png
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build/pyinstaller packaging/pcdeck.spec

case "$(uname -s)" in
Darwin)
    APP="dist/PC Control Deck.app"
    OUT="dist/release/PCControlDeck-$VERSION-macos-$ARCH"
    rm -f "$OUT.zip" "$OUT.dmg"
    ditto -c -k --sequesterRsrc --keepParent "$APP" "$OUT.zip"
    STAGE=$(mktemp -d)
    cp -R "$APP" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    # hdiutil sometimes fails with "Resource busy" on CI runners: retry a few times.
    for i in 1 2 3 4 5; do
        if hdiutil create -volname "PC Control Deck $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$OUT.dmg"; then
            break
        fi
        [ "$i" = 5 ] && exit 1
        sleep 5
    done
    rm -rf "$STAGE"
    ;;
Linux)
    NAME="PCControlDeck-$VERSION-linux-$ARCH"
    STAGE="build/$NAME"
    rm -rf "$STAGE"
    mkdir -p "$STAGE"
    cp -R dist/PCControlDeck "$STAGE/"
    cp packaging/linux/pcdeck.desktop packaging/linux/install.sh build/pcdeck.png "$STAGE/"
    chmod +x "$STAGE/install.sh" "$STAGE/PCControlDeck/PCControlDeck"
    tar -C build -czf "dist/release/$NAME.tar.gz" "$NAME"
    ;;
*)
    echo "unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac
ls -l dist/release
