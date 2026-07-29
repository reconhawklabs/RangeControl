#!/usr/bin/env bash
# Build the Windows x64 one-file binary using Windows Python under Wine.
#
# PyInstaller does not cross-compile: it bundles the interpreter and compiled
# extension modules of the platform it runs on. Running a genuine Windows
# Python under Wine gives it real win_amd64 artifacts to bundle. Every
# RangeControl dependency publishes win_amd64 wheels, so nothing is built
# from source here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="docker.io/tobix/pywine:3.12"

podman run --rm -v "$ROOT:/src:z" -w /src "$IMAGE" bash -c '
set -euo pipefail
# tobix/pywine ships tkinter under Wine; verify rather than assume, because a
# missing Tk produces a binary that fails only when the window is opened.
wine python -c "import tkinter; print(\"tk\", tkinter.TkVersion)"
wine python -m pip install --quiet --no-cache-dir . "pyinstaller>=6.21"
cd packaging
wine pyinstaller --clean --noconfirm \
    --distpath ../dist/windows --workpath ../build/windows \
    rangecontrol.spec
'

# tobix/pywine runs the container as root, and rootless podman maps that back
# to the invoking host user by default (same reasoning as build-linux.sh), so
# this normally never fires. Kept as a guard rather than assumed away.
if [ "$(stat -c '%u' "$ROOT/dist/windows/rangecontrol.exe")" != "$(id -u)" ]; then
    echo "dist/build owned by another uid; reclaiming via podman unshare" >&2
    podman unshare chown -R "$(id -u):$(id -g)" "$ROOT/build" "$ROOT/dist"
fi

echo "Built: $ROOT/dist/windows/rangecontrol.exe"
ls -lh "$ROOT/dist/windows/rangecontrol.exe"
