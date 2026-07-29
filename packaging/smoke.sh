#!/usr/bin/env bash
# Prove a built binary actually works. A green pytest run says nothing about
# a frozen bundle: missing package data, missed hidden imports, and provider
# SDKs that fail to survive freezing are visible only here. The checks
# themselves live in packaging/smoke-checks.sh, shared with
# packaging/smoke-windows.sh so both binaries are held to the same standard.
set -euo pipefail

BINARY="${1:?usage: smoke.sh path/to/binary}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp -r "$ROOT/tests/fixtures/range/." "$WORK/"
cp "$BINARY" "$WORK/rangecontrol"

cd "$WORK"
# shellcheck source=packaging/smoke-checks.sh
source "$ROOT/packaging/smoke-checks.sh"

# Run with a completely empty environment: verify-bundle is dispatched before
# Config is loaded specifically so it needs no Discord token, LLM provider,
# API key, or channel ID, and this is where that stays proven rather than
# accidentally masked by the credentials normal_run below needs.
verify_run() { env -i ./rangecontrol "$@"; }

normal_run() {
    LLM_PROVIDER=anthropic LLM_API_KEY=smoke-not-used \
    DISCORD_BOT_TOKEN=smoke WHITE_CELL_CHANNEL_ID=1 RANGE_DIR="$WORK" \
    ./rangecontrol "$@"
}

run_smoke_checks

# Narrow claim only: this proves argparse and the frozen CLI plumbing survive
# freezing. It does NOT exercise Tk or the gui/ hidden imports -- --help takes
# the argparse branch in main() and never imports rangecontrol.gui.app, so a
# missing Tk would not fail here (confirmed experimentally: stubbing tkinter
# to None in sys.modules and calling main(["--help"]) still exits 0). The Tk
# and hidden-import guard lives in run_smoke_checks above, via
# --verify-bundle importing rangecontrol.gui.app.
./rangecontrol --help > help.txt 2>&1 || {
    echo "FAIL: --help exited non-zero"; cat help.txt; exit 1; }
grep -q "rangecontrol" help.txt || { echo "FAIL: unexpected --help"; exit 1; }

echo "PASS: $BINARY"
