#!/bin/bash
# Build MIDI Staff Visualizer as a macOS .app bundle
set -e
cd "$(dirname "$0")"

echo "==> Installing / updating dependencies..."
python3.11 -m pip install --upgrade pygame mido python-rtmidi pyinstaller

echo "==> Running PyInstaller..."
python3.11 -m PyInstaller \
    --windowed \
    --name "MIDI Staff Visualizer" \
    --add-data "valse_69_1_(c)dery.mid:." \
    --osx-bundle-identifier "com.sqapp.midistaffvisualizer" \
    --noconfirm \
    main.py

echo ""
echo "==> Done! App is at: dist/MIDI Staff Visualizer.app"
echo "    Copy it to /Applications or double-click from dist/ to launch."
