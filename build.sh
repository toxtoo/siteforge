#!/bin/bash
set -e

# Stop early if the help file is missing, so it is not left out of the build
if [ ! -f "help/help.html" ]; then
    echo "ERROR: help/help.html not found. Put the help folder next to this script."
    exit 1
fi

# Stop early, with instructions, if the build tools have not been set up yet
if [ ! -x "./venv/bin/python3" ]; then
    echo "ERROR: ./venv not found. Set it up once with:"
    echo "    python3 -m venv venv"
    echo "    ./venv/bin/pip install PyQt6 PyQt6-QScintilla pyinstaller"
    exit 1
fi

echo "--- Cleaning previous build artifacts ---"
rm -rf build dist siteforge.spec

echo "--- Activating virtual environment & building with PyInstaller ---"
./venv/bin/python3 -m PyInstaller \
    --onefile \
    --noconsole \
    --name siteforge \
    --icon="icons/siteforge-icon.png" \
    --add-data "icons:icons" \
    --add-data "help:help" \
    siteforge.py

echo "--- Build complete! ---"
ls -lh dist/siteforge
