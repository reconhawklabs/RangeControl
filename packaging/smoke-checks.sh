# Checks shared by packaging/smoke.sh (native Linux) and
# packaging/smoke-windows.sh (the same binary under Wine), so both prove
# identical behavior against their respective binaries instead of maintaining
# two copies of the same assertions that can silently drift apart.
#
# This file is meant to be `source`d, not run directly -- it defines a
# function and relies on the caller's shell options (set -euo pipefail) and
# working directory. It has no shebang and is not executable for that reason.
#
# Callers must define two functions before calling run_smoke_checks:
#
#   verify_run <args...>
#       Runs the binary with no RangeControl configuration present at all
#       (no Discord token, LLM provider, API key, or channel ID). May still
#       carry whatever the OS/runtime itself needs to start the process
#       (e.g. Wine's WINEPREFIX) -- that is plumbing, not application config.
#
#   normal_run <args...>
#       Runs the binary with a full RangeControl configuration applied
#       (LLM_PROVIDER, LLM_API_KEY, DISCORD_BOT_TOKEN, WHITE_CELL_CHANNEL_ID,
#       RANGE_DIR).
#
# Both must send stdout+stderr to whatever the caller redirects run_smoke_checks's
# own invocations to. Only exit codes and specific expected substrings are
# checked here -- never "stderr is empty" -- because Wine emits its own
# window-driver/systray/vulkan warnings on stderr from this headless
# container's absent display, and that noise must not be mistaken for a
# RangeControl failure.
run_smoke_checks() {
    # cache-status and --dry-run never call load_template(): the fixture
    # already has a Range.md, so generate_range_md() (the only caller of
    # load_template()) never runs. --dry-run does call build_provider(), but
    # only for whichever provider normal_run's LLM_PROVIDER selects (below:
    # anthropic) -- Gemini was never exercised inside a frozen bundle by any
    # check until --verify-bundle started constructing one provider per
    # rangecontrol.config.VALID_PROVIDERS, ahead of Config validation, below.
    # This proves the base anthropic/google-genai packages import and
    # construct a client inside the frozen bundle; it does NOT prove every
    # rangecontrol.spec collect_all hidden-import or data-file entry is
    # load-bearing -- a submodule reachable only through an actual
    # request/response cycle would not be caught here.
    #
    # --verify-bundle also imports rangecontrol.gui.app (without calling
    # run_gui()), which is the only place in this whole script that touches
    # Tk at all: --help below takes the argparse branch in main() and never
    # imports the GUI module, so it cannot catch a missing Tk or a missed
    # gui/ hidden import despite the comment that used to claim it did.
    verify_run --verify-bundle > out.txt 2>&1 || {
        echo "FAIL: verify-bundle exited non-zero"; cat out.txt; return 1; }
    grep -q "RangeTemplate.md loaded from the bundle" out.txt || {
        echo "FAIL: no bundle-template confirmation"; cat out.txt; return 1; }
    grep -q "OK: CA store loaded" out.txt || {
        echo "FAIL: no CA store in the bundle -- TLS will fail on this platform"
        cat out.txt; return 1; }

    grep -q "OK: GUI module imported" out.txt || {
        echo "FAIL: gui/app.py (Tk and its hidden imports) was never verified inside the bundle"
        cat out.txt; return 1; }
    grep -q "OK: anthropic provider constructed" out.txt || {
        echo "FAIL: anthropic provider was never verified inside the bundle"
        cat out.txt; return 1; }
    grep -q "OK: gemini provider constructed" out.txt || {
        echo "FAIL: gemini provider was never verified inside the bundle"
        cat out.txt; return 1; }

    normal_run --dry-run >> out.txt 2>&1 || {
        echo "FAIL: --dry-run exited non-zero"; cat out.txt; return 1; }
    grep -q "RangeControl ingest report" out.txt || {
        echo "FAIL: no ingest report"; cat out.txt; return 1; }
    grep -q "Range.md:" out.txt || { echo "FAIL: no Range.md line"; cat out.txt; return 1; }

    normal_run cache-status >> out.txt 2>&1 || {
        echo "FAIL: cache-status exited non-zero"; cat out.txt; return 1; }
}
