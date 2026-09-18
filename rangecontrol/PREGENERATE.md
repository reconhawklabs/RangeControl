# Pre-generating a RangeControl folder

These instructions are for a coding agent (Claude Code or equivalent) working
in a RangeControl folder that has a populated `resources/` directory but no
`Range.md` and no `.rangecontrol/cache/`.

Your job is to produce both, so the operator can start the bot and have it come
up with everything already in place: no extraction, no vision calls, no
`Range.md` generation at launch.

## Prerequisites

The `rangecontrol` (Linux) or `rangecontrol.exe` (Windows) binary that
created this folder is sitting right next to it. Every command below is
that same binary, given an argument, which is all it takes to make it run as
a CLI instead of opening its window (see the README's "Command line"
section). No install step, no Python, no virtualenv needed:

```bash
./rangecontrol cache-status
```

On Windows, the equivalent is `rangecontrol.exe cache-status` (see the
Windows note just below before relying on its output).

If you are instead working from a source checkout with a Python toolchain
available, run `pip install -e .` once and use the plain `rangecontrol`
command in place of `./rangecontrol` for every step below. It is the exact
same CLI either way.

**Windows console output.** The packaged `rangecontrol.exe` is built with no
console window, so it can open silently as a GUI when double-clicked. That
also means `cache-status` and `cache-put` may print nothing to the screen
when the `.exe` is run directly from Command Prompt or PowerShell. This is
a known limitation of windowed Windows executables. Redirect to a file and
open that instead:

```
rangecontrol.exe cache-status > status.txt
```

then read `status.txt`. This was verified to work under Wine against the
actual packaged binary, but Wine is not proof of real Windows behavior in
this specific area, so treat it as best-effort until confirmed on a real
Windows machine. If redirection turns out not to work for you, the
pip-installed CLI (`pip install -e .` from a checkout, as above) is a normal
console application and does not have this limitation at all. Use it
instead if you have a checkout and a Python toolchain available.

## Step 1: see what needs extracting

```bash
./rangecontrol cache-status
```

This lists every discovered resource and whether it already has a cache entry.
Work through the "Not yet cached" list.

## Step 2: extract each resource and record it

For each uncached file, read it yourself and write out its full text content.
You will generally extract more faithfully than the bot's built-in extractors,
so this step is the main reason to pre-generate:

- **PDFs**: transcribe all text, preserving tables and page structure.
- **Images and diagrams**: describe topology, trust boundaries, device roles,
  and arrow directions, and transcribe every visible label, hostname, address,
  and port **verbatim**. Do not summarise.
- **Spreadsheets**: one line per row, sheet names preserved.
- **Documents and slide decks**: full text including tables, speaker notes,
  and a description of every embedded diagram or screenshot.
- **Scripts and configs**: verbatim.

Then record it:

```bash
./rangecontrol cache-put resources/net/diagram.png --text-file /tmp/diagram.txt
```

`--text-file` is preferred over `--text` for anything multi-line. Re-run
`./rangecontrol cache-status` as you go; keep working until `Missing: 0`.

**Do not hand-write files into `.rangecontrol/cache/`.** The entry key is
derived from the resource's content hash and the extractor version. `cache-put`
computes it; a hand-written file will silently never be found, and the bot will
re-extract that resource at startup.

## Step 3: author Range.md

`Range.md` must contain every section below, in this order. There is no
`RangeTemplate.md` file to read in this folder. It ships inside the binary
itself and is never written to disk, so the structure it defines is
reproduced here instead. (Working from a source checkout instead of a
packaged binary? The same structure also lives at
`rangecontrol/RangeTemplate.md`, with a short description under each
heading.)

```
# Range Definition

## Exercise Overview
## Network Topology
## Asset Inventory
## Required Access Paths
## Firewall & Policy Baseline
## Users & Accounts
## Automation & Scripts
## MSEL & Inject Catalog
## Attack Path Dependencies
## Scoring & Availability Requirements
## Intentional Vulnerabilities
## Out of Scope / Do Not Touch
## Standing Adjudication Rules
## Protected Dependency Index
## Ingest Gaps
```

Read every extracted resource (your own extractions from Step 2) and fill the
template in. Three sections carry the most weight:

- **Required Access Paths**: connectivity that must keep working for the
  exercise to proceed.
- **MSEL & Inject Catalog**: every inject, with its prerequisites and the
  assets, ports, and paths it depends on.
- **Protected Dependency Index**: the flat table mapping each protected
  element to what breaks if it becomes blocked, disabled, or unreachable.
  Every inject and attack-path entry must declare its dependencies into this
  table. This is what makes adjudication a lookup rather than a re-derivation
  of the whole scenario, so do not leave it thin.

Record anything you could not determine under **Ingest Gaps** rather than
guessing. A stated gap is safe; an invented fact produces wrong rulings.

Write the result to `Range.md` in this folder.

## Step 4: verify the handoff

```bash
./rangecontrol --dry-run
```

This loads `Range.md`, rebuilds the corpus from your cache, and prints the
ingest report without connecting to Discord. Confirm:

- The report shows no unreadable files.
- Asset, inject, and Protected Dependency Index counts match what you wrote.
- No API calls were needed: a fully populated cache plus an existing
  `Range.md` means startup touches no provider.

The operator can now start the bot normally.

## Do not

- Do not commit `Range.md`, `resources/`, or `.rangecontrol/`. All three are
  gitignored, and a generated scenario file is the most damaging thing that
  could end up in a repository.
- Do not put scenario content in any other file. `Range.md` and the cache are
  the only outputs.
