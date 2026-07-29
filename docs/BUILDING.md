# Building the RangeControl binaries

This covers producing the one-file `rangecontrol` (Linux x64) and
`rangecontrol.exe` (Windows x64) executables from a checkout. It is for
maintainers and contributors — end users just run the binary; see the
[README](../README.md) for that.

## Prerequisite: podman

Both builds run inside a container, driven by `podman`. You need it
installed and working (`podman --version`); nothing else is required on the
host — no local Python toolchain, no Wine, no cross-compiler.

```bash
podman --version
```

The two images used are pulled automatically on first use if not already
present locally:

- `docker.io/library/python:3.12-bookworm` (Linux build)
- `docker.io/tobix/pywine:3.12` (Windows build)

## Building the Linux binary

```bash
./packaging/build-linux.sh
```

This runs `pip install .` and `pyinstaller` inside a Debian bookworm
container and writes the result to `dist/linux/rangecontrol` (~54 MB).

### Why bookworm, and not just the build host

A binary produced by PyInstaller links against the glibc on the machine that
built it, and only runs on a target with an equal or newer glibc. Building
directly on the host — whatever distribution that happens to be — bakes in
whatever glibc that host ships, which may be newer than what you need to
support. Debian bookworm ships glibc 2.36, which is old enough to run on
Ubuntu 22.04 and RHEL 9 (both released after bookworm's glibc), while still
being new enough for every dependency RangeControl uses. Building in a
pinned bookworm container, rather than on whatever the developer's or CI
runner's host distribution happens to be, is what makes that floor
consistent and reproducible build to build.

## Building the Windows binary

```bash
./packaging/build-windows.sh
```

This writes `dist/windows/rangecontrol.exe` (~36 MB).

### Why this runs under Wine

PyInstaller does not cross-compile. It bundles the interpreter and compiled
extension modules of whatever platform it is *running on* — it cannot be run
on Linux and asked to produce a Windows build directly. To get a real
`win_amd64` binary, PyInstaller has to actually execute on a Windows Python.
`tobix/pywine` provides exactly that: a genuine Windows Python distribution
running under Wine inside a Linux container, so `wine python` and
`wine pyinstaller` behave as if a real Windows machine were building the
binary. Every RangeControl dependency ships prebuilt `win_amd64` wheels, so
nothing needs to compile from source under Wine — only pure-Python /
prebuilt-wheel installs happen there.

## Verifying a build: smoke tests

A green `pytest` run says nothing about whether a *frozen* binary actually
works — missing package data, a hidden import PyInstaller's static analysis
missed, or a provider SDK that doesn't survive freezing are all invisible to
the test suite and only show up in the built artifact. Run the matching
smoke script after every build:

```bash
./packaging/smoke.sh dist/linux/rangecontrol
```

```bash
./packaging/smoke-windows.sh dist/windows/rangecontrol.exe
```

`smoke.sh` runs the Linux binary directly. `smoke-windows.sh` runs the
Windows binary under Wine, inside the same `tobix/pywine` container the
build itself uses — there is no other way to execute a `.exe` on this build
host. Both scripts share their actual assertions from
`packaging/smoke-checks.sh`, so the two binaries are held to identical
checks rather than two hand-maintained copies that can quietly drift apart:
they exercise `--verify-bundle` (which imports the GUI module and
constructs a client for every registered provider, all with dummy
credentials and no network calls), `--dry-run` against a fixture range, and
`cache-status`.

Neither smoke script makes a real API call. Constructing a provider client
is not the same as calling it, and the fixture run uses a placeholder API
key that is never sent anywhere.

## Adding a hidden import for a new dependency

`packaging/rangecontrol.spec` is the single spec file shared by both builds.
If you add a new third-party dependency to `pyproject.toml`, check whether
PyInstaller's static analysis actually finds everything it needs:

1. Add the dependency normally, then run a build (`./packaging/build-linux.sh`
   is faster to iterate on than the Windows one).
2. Run the smoke test. A dependency that imports submodules lazily,
   or that ships non-Python data files it reads at runtime (translation
   files, schemas, model definitions), will typically pass `--help` but fail
   the first real use of that dependency — `--verify-bundle`'s
   provider-construction check exists specifically to catch this for the two
   provider SDKs, so a new provider SDK should get an equivalent construction
   check there, not just a spec-file entry.
3. If something is missing, add the package name to the `collect_all(...)`
   loop near the top of `rangecontrol.spec`:

   ```python
   for package in ("anthropic", "google.genai", "discord", "pypdf",
                   "openpyxl", "docx", "your_new_package"):
       pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
       datas += pkg_datas
       binaries += pkg_binaries
       hiddenimports += pkg_hidden
   ```

   `collect_all` is used instead of a bare `hiddenimports` entry because it
   also picks up non-Python data files the package needs (this is exactly
   why `anthropic` and `google-genai` are already there — both carry pydantic
   model data and import submodules lazily, which PyInstaller's static
   analysis alone does not follow).
4. Rebuild and re-run the smoke test to confirm the failure is gone.

If a dependency needs something more specific than `collect_all` provides
(a single missing submodule, say), add it to the plain `hiddenimports` list
instead — `collect_all` is the default because it is broad enough to cover
undiscovered cases without hand-auditing a new dependency's internals, not
because every dependency actually needs its full weight.
