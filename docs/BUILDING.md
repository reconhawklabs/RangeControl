# Building the RangeControl binaries

For maintainers. End users just run the binary; see the [README](../README.md).

## Prerequisite

`podman`. Nothing else is needed on the host. The build images
(`python:3.12-bookworm` for Linux, `tobix/pywine:3.12` for Windows) are
pulled on first use.

## Build

```bash
./packaging/build-linux.sh      # -> dist/linux/rangecontrol
./packaging/build-windows.sh    # -> dist/windows/rangecontrol.exe
```

Linux builds in Debian bookworm so the binary runs on anything with
glibc 2.36 or newer. Windows builds under Wine because PyInstaller cannot
cross-compile.

## Smoke test

Run after every build. A passing `pytest` says nothing about the frozen binary.

```bash
./packaging/smoke.sh dist/linux/rangecontrol
./packaging/smoke-windows.sh dist/windows/rangecontrol.exe
```

Both run `--verify-bundle`, `--dry-run` against a fixture range, and
`cache-status`. No real API calls are made.

## New dependencies

`packaging/rangecontrol.spec` is shared by both builds. If a new dependency
fails in the frozen binary but works in tests, add its package name to the
`collect_all(...)` loop at the top of the spec, rebuild, and re-run the
smoke test.
