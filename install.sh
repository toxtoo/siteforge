#!/bin/bash
#
# SiteForge installer (Linux)
#
#   ./install.sh              install SiteForge for the current user
#   ./install.sh --uninstall  remove it again
#
# Nothing here needs administrator rights. Everything goes into your own
# home folder:
#   ~/.local/share/siteforge/                  the program and its icon
#   ~/.local/share/applications/siteforge.desktop   the menu launcher

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HOME/.local/share/siteforge"
MENUDIR="$HOME/.local/share/applications"
LAUNCHER="$MENUDIR/siteforge.desktop"

refresh_menu() {
    # Tell the desktop to notice the new (or removed) launcher, if it can
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$MENUDIR" >/dev/null 2>&1 || true
    fi
}

if [ "$1" = "--uninstall" ]; then
    rm -rf "$APPDIR"
    rm -f "$LAUNCHER"
    refresh_menu
    echo "SiteForge has been removed from the menu and from $APPDIR."
    echo "Your settings in ~/.config/SiteForge were left alone."
    exit 0
fi

if [ ! -f "$HERE/siteforge" ]; then
    echo "ERROR: the 'siteforge' program was not found next to install.sh."
    echo "Run install.sh from inside the folder you extracted."
    exit 1
fi

if [ ! -f "$HERE/siteforge-icon.png" ]; then
    echo "ERROR: 'siteforge-icon.png' was not found next to install.sh."
    exit 1
fi

mkdir -p "$APPDIR" "$MENUDIR"

cp "$HERE/siteforge" "$APPDIR/siteforge"
cp "$HERE/siteforge-icon.png" "$APPDIR/siteforge-icon.png"
chmod +x "$APPDIR/siteforge"
chmod 644 "$APPDIR/siteforge-icon.png"

# Write the launcher with the correct paths for this computer
cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Type=Application
Name=SiteForge
Comment=HTML editor
Exec="$APPDIR/siteforge"
Icon=$APPDIR/siteforge-icon.png
Terminal=false
Categories=Development;WebDevelopment;TextEditor;
EOF

chmod 644 "$LAUNCHER"
refresh_menu

echo "SiteForge is installed."
echo
echo "Look for SiteForge in your applications menu (under Programming or"
echo "Development). If it isn't there yet, log out and back in."
echo
echo "To remove it later, run:  ./install.sh --uninstall"
