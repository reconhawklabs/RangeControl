#!/usr/bin/env bash
# Build the Linux x64 one-file binary inside Debian bookworm.
#
# Built in a container rather than on the host on purpose: a binary linked
# against Fedora's glibc refuses to start on Ubuntu 22.04 or RHEL 9 with
# "GLIBC_2.xx not found". Bookworm's glibc 2.36 covers everything from 2023 on.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="docker.io/library/python:3.12-bookworm"

podman run --rm -v "$ROOT:/src:z" -w /src "$IMAGE" bash -c '
set -euo pipefail
# The full python image ships tkinter; verify rather than assume, because a
# missing Tk produces a binary that fails only when the window is opened.
python -c "import tkinter; print(\"tk\", tkinter.TkVersion)"
pip install --quiet --no-cache-dir . "pyinstaller>=6.21"
cd packaging
pyinstaller --clean --noconfirm \
    --distpath ../dist/linux --workpath ../build/linux \
    rangecontrol.spec
'

# Rootless podman maps the container's root (the user pip installs as) back to
# the invoking host user by default, so build/ and dist/ normally land owned
# by that user already. --userns=keep-id would guarantee it more explicitly,
# but it also makes the container process non-root, and pip then cannot write
# into the image's system site-packages ("Check the permissions."). This
# fallback only fires if a rootless setup ever maps ownership differently.
if [ "$(stat -c '%u' "$ROOT/dist/linux/rangecontrol")" != "$(id -u)" ]; then
    echo "dist/build owned by another uid; reclaiming via podman unshare" >&2
    podman unshare chown -R "$(id -u):$(id -g)" "$ROOT/build" "$ROOT/dist"
fi

echo "Built: $ROOT/dist/linux/rangecontrol"
ls -lh "$ROOT/dist/linux/rangecontrol"
