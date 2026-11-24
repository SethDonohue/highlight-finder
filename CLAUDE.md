# Highlight Finder: instructions for Claude

Personal tool that sorts GoPro and Samsung phone trip footage: flags junk, proposes highlights, and produces a DaVinci Resolve rough cut (and later a rendered final edit). Python CLI + local web review UI. It runs on the owner's M1 Mac, where the footage lives on external drives. The owner mostly works from a phone, so explain things simply and briefly.

Design: the "Trip Footage Highlight Finder - Program Spec" (kept in the owner's Claude account, not in this repo). Operating it on a trip is covered by the `trip-footage-review` skill.

## Stack
- Python 3.10+ (3.12 on the Mac), standard library HTTP server, SQLite
- numpy, Pillow, OpenTimelineIO; ffmpeg/ffprobe on PATH (VideoToolbox on the Mac)
- Review UI: one vanilla HTML/JS page in `hlf/web/index.html`

## How we work
- One phase or feature at a time (see "Build phases" in the spec). After each one, publish a release so it can be tried on the Mac before moving on.
- Before multi-file changes, give a short plain-language plan and wait for an OK.
- Make minimal, focused changes. Follow existing patterns and naming.
- Ask before adding any new dependency. Free and open-source only: no paid APIs or services.
- Never claim something works until tests pass. Tell the owner exactly what to try on the Mac.

## Footage rules (non-negotiable)
- Originals are read-only: never write, move, rename or delete anything next to them.
- Nothing is rejected, deleted or exported without the owner's tap; Claude's picks are notes and suggestions.
- No uploads: footage, frames and audio stay on the Mac. No analytics.
- Never commit real footage, frames or personal data. Test footage is generated with ffmpeg in `tests/`.

## Build and release
- Before releasing: `python -m pytest -q tests` passes (needs ffmpeg; the end-to-end test generates made-up clips).
- Steps: bump `__version__` in `hlf/__init__.py` (release tag is `v<__version__>`; patch for fixes, minor for features) → update README "Status", `CHANGELOG.md` and `TEST-CHECKLIST.md` → commit and push to `main` → start the workflow with `gh api -X POST repos/SethDonohue/highlight-finder/actions/workflows/release.yml/dispatches -f ref=main` → watch it (`gh run list --workflow release.yml`) → confirm the release and its wheel exist.
- The workflow tests on Linux, builds the wheel and a source zip, and publishes the GitHub Release. It refuses a tag that doesn't match `__version__` or already exists.
- Always send the owner the link to the release page, the one-line install/upgrade command, and what to test on the Mac.
- Guided mode (`hlf` alone) lives in `hlf/guide.py`; keep it in step with new commands and phases (for example, wire the Final cut menu item when Phase 4 lands).
- Install or upgrade on the Mac: `curl -fsSL https://raw.githubusercontent.com/SethDonohue/highlight-finder/main/install.sh | bash` (`install.sh` in this repo; it installs into `~/.highlight-finder/venv` and links `hlf` into Homebrew's bin). The working folder `~/HighlightFinder` and its index survive upgrades.

## Data
- `index.db` schema changes must be additive (new tables/columns with defaults) or come with a migration; never drop the owner's decisions, ratings or notes.
- Settings changes go through `hlf set ... --reason` so `tuning-log.md` keeps the history.

## Commits
- Author every commit as the owner's GitHub identity: `git config user.name "Seth Donohue"` and `git config user.email "31681651+SethDonohue@users.noreply.github.com"` (check with `git config user.email` before the first commit in a session; never commit as "Claude").
- End commit messages with the attribution lines the session provides.
