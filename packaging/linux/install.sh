#!/bin/sh
# Per-user install of the Control Deck Linux build (no root needed).
#   ./install.sh             copy to ~/.local/opt/pcdeck, add the menu entry and icon
#   ./install.sh --uninstall remove them (the paired-device store in ~/.config/pcdeck is kept)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$HOME/.local/opt/pcdeck"
DESKTOP="$DATA/applications/pcdeck.desktop"
ICON="$DATA/icons/hicolor/256x256/apps/pcdeck.png"

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$DEST" "$DESKTOP" "$ICON" "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/pcdeck.desktop"
    echo "Control Deck removed."
    exit 0
fi

mkdir -p "$DEST" "$(dirname "$DESKTOP")" "$(dirname "$ICON")"
rm -rf "$DEST/PCControlDeck"
cp -R "$HERE/PCControlDeck" "$DEST/"
cp "$HERE/pcdeck.png" "$ICON"
sed "s|@EXEC@|\"$DEST/PCControlDeck/PCControlDeck\"|" "$HERE/pcdeck.desktop" > "$DESKTOP"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" || true
echo "Installed to $DEST. Start \"Control Deck\" from the applications menu."
