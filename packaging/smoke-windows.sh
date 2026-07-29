#!/usr/bin/env bash
# Windows counterpart to packaging/smoke.sh: proves dist/windows/rangecontrol.exe
# survives freezing, not just dist/linux/rangecontrol. Runs the same checks
# (packaging/smoke-checks.sh) under Wine, inside the same podman image
# build-windows.sh uses -- Wine only exists there, and this container has no
# binfmt_misc registration for PE binaries, so the .exe cannot be invoked
# directly on the host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="docker.io/tobix/pywine:3.12"
BINARY="${1:-$ROOT/dist/windows/rangecontrol.exe}"

if [ ! -f "$BINARY" ]; then
    echo "FAIL: $BINARY not found" >&2
    exit 1
fi

# podman only mounts $ROOT (as /src below), so the binary under test must
# live inside the repository tree for the container to see it at all. Resolve
# to an absolute path first so a relative $1 (or the default) is handled the
# same way, then re-express it relative to $ROOT and pass that in explicitly
# -- a hardcoded path here would silently test a different binary than the
# one the caller asked for and just passed the existence check above.
BINARY_ABS="$(cd "$(dirname "$BINARY")" && pwd)/$(basename "$BINARY")"
case "$BINARY_ABS" in
    "$ROOT"/*)
        BINARY_REL="${BINARY_ABS#"$ROOT"/}"
        ;;
    *)
        echo "FAIL: $BINARY must be inside $ROOT (podman only mounts the repo tree)" >&2
        exit 1
        ;;
esac

podman run --rm -v "$ROOT:/src:z" -w /src -e BINARY_REL="$BINARY_REL" "$IMAGE" bash -c '
set -euo pipefail
WORK="$(mktemp -d)"
trap "rm -rf \"$WORK\"" EXIT

cp -r tests/fixtures/range/. "$WORK/"
cp "$BINARY_REL" "$WORK/rangecontrol.exe"
cd "$WORK"

# shellcheck source=packaging/smoke-checks.sh
source /src/packaging/smoke-checks.sh

# Wine itself needs WINEPREFIX, PATH (to find wine on $PATH), and HOME (Wine
# falls back to $HOME/.wine and would silently bootstrap a second prefix
# without it) -- OS/Wine plumbing, not RangeControl configuration. Every
# application env var (Discord token, LLM provider, API key, channel ID) is
# still zeroed by env -i, preserving the actual claim under test:
# --verify-bundle needs none of them, on Windows any more than on Linux.
verify_run() {
    env -i HOME="$HOME" WINEPREFIX="${WINEPREFIX:-$HOME/.wine}" PATH="$PATH" \
        wine rangecontrol.exe "$@"
}

normal_run() {
    LLM_PROVIDER=anthropic LLM_API_KEY=smoke-not-used \
    DISCORD_BOT_TOKEN=smoke WHITE_CELL_CHANNEL_ID=1 RANGE_DIR="$WORK" \
    wine rangecontrol.exe "$@"
}

run_smoke_checks
echo "PASS: $BINARY_REL"
'
