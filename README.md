# Highlight Finder

Finds the best moments in GoPro and phone trip footage, flags junk, and hands you a rough cut for DaVinci Resolve. You make every final call: nothing is rejected, deleted or exported without your tap, and your original files are only ever read.

## Status

**v0.1.0**: spec phases 0–2. Ingest, GoPro telemetry, junk triage, highlight proposals, review UI (Mac and phone), DaVinci Resolve rough cut. Next: AI signals (search, sound events, transcripts), then the rendered final edit. See `CHANGELOG.md` and `TEST-CHECKLIST.md`.

## Install on the M1 Mac (once, about 10 minutes)

1. Install [Homebrew](https://brew.sh) if you don't have it, then:
   ```
   brew install ffmpeg python@3.12 gh
   gh auth login
   ```
2. Make a home for the program and install the latest release:
   ```
   python3.12 -m venv ~/hlf-venv
   source ~/hlf-venv/bin/activate
   gh release download -R SethDonohue/highlight-finder -p '*.whl' -D /tmp/hlf --clobber
   pip install --upgrade /tmp/hlf/*.whl
   hlf doctor
   ```
   `doctor` should say ffmpeg, ffprobe, videotoolbox and otio are ok. Run `source ~/hlf-venv/bin/activate` in each new terminal before using `hlf`.

**Upgrading** to a new release: the same `gh release download ... && pip install --upgrade ...` lines (add the tag, e.g. `gh release download v0.2.0 ...`, for a specific version). Your working folder and decisions are kept.

The working folder (index, proxies, frames, exports) is `~/HighlightFinder`. Put it elsewhere with `--home /Volumes/Work/HighlightFinder` or `export HLF_HOME=...`. It needs roughly 1 GB per hour of footage.

## Use it on a trip

```
hlf run --trip "Whistler 2026" --profile mtb "/Volumes/GoPro SSD/Whistler" "/Volumes/GoPro SSD/Phone"
```

- Profiles: `mtb`, `snowboard`, `pickleball`, `travel`. They change how motion, sound and looks are weighted.
- `run` = `ingest` + `analyze` + `score`. It's resumable: if the Mac sleeps or a drive unplugs, run it again and it picks up where it stopped.
- GoPro chapters (GX01…, GX02… of one recording) are stitched into one clip. `.LRV` low-res copies are used for faster decoding when they still exist.
- To process overnight and survive closing the terminal: `nohup hlf run --trip ... > ~/HighlightFinder/run.log 2>&1 &`

Then review:

```
hlf serve          # on the Mac: opens at http://127.0.0.1:8765
hlf serve --lan    # also from your phone on the same Wi-Fi: open the link it prints
```

- **Highlights:** each proposal loops inside its in/out points. Keep, Reject, nudge start/end by 0.5 s, rate 1–5, tag, note. On the phone, swipe right to keep and left to reject. On the Mac: K, X, `[` `]`, `{` `}`, 1–5, Space.
- **Junk:** clips the program thinks are accidental (too short, lens covered, frozen, blurry), with thumbnails and the reason. Confirm in bulk or rescue any clip.
- **Export:** writes the Resolve rough cut.

## The Resolve rough cut

```
hlf export roughcut --trip "Whistler 2026"
```

Writes to `~/HighlightFinder/exports/<trip>/`:

- `<trip>_rough_cut.otio`: kept segments in story order on V1, camera audio on A1, a marker on each clip with the caption, why line, rating and tags. In Resolve: **File > Import > Timeline**, pick the .otio. It links to your originals, so all the footage around each cut is there to extend trims.
- `manifest.json` / `.csv`: every segment with source file and in/out times.
- `telemetry/segment_<id>.csv`: speed, g-force and airtime for each kept segment, for overlays later.

Optional: `hlf export clips --trip ...` writes each kept segment as a lossless MP4 (stream copy: instant, no quality loss, cuts snap to keyframes).

Free Resolve outputs up to UHD (3840×2160); 5.3K sources import fine. If 10-bit GoPro files won't play in free Resolve, tell Claude so the export can link 8-bit proxies instead.

## What it looks at

| Signal | GoPro | Phone |
| --- | --- | --- |
| Telemetry: vibration and rotation (action), airtime (jumps), GPS speed, read straight from the file's GPMF track | yes | no |
| HiLight tags you pressed while filming | yes | no |
| On-screen motion, exposure, frozen frames (from a tiny 4 fps copy) | yes | yes |
| Sharpness (1 frame per second) | yes | yes |
| Sudden loud moments vs. the surrounding 30 s (cheers, yells, not steady wind) | yes | yes |

Signals are ranked within the trip, so a calm day isn't judged against a bike-park day. Peaks grow into 4–20 s segments with 2 s lead-in and 1.5 s tail. Each gets a "why" line. Long stretches with nothing happening are skipped but never deleted.

## Tuning

```
hlf settings                                   # everything, as JSON
hlf set segments.peak_pct 80 --reason "too few proposals on the Whistler trip"
hlf score --trip "Whistler 2026"               # re-score; your keep/reject choices are kept
hlf reset-settings --reason "start over"
```

Every change is logged in `~/HighlightFinder/tuning-log.md` with old value, new value and reason.

## Commands Claude uses when operating it

```
hlf status --trip T                     # counts as JSON
hlf list --trip T --limit 40 --json     # top proposals
hlf frames --segment 12 --n 4 --out /tmp/look   # a few frames to judge a moment
hlf note --segment 12 --caption "Drop into the rock garden" --claude-note "Best opener; trim 1 s off the start"
```

`note` adds captions and editorial notes only; it never keeps or rejects anything.

## Tests and releases

```
pip install -e . pytest && python -m pytest -q tests
```

The end-to-end test makes its own fake footage with ffmpeg; no real footage is ever committed. Releases are built by GitHub Actions (`.github/workflows/release.yml`): bump `__version__` in `hlf/__init__.py`, push to `main`, then Actions → Release → Run workflow. It tests, builds the wheel and publishes the release.

The telemetry reader has been checked against GoPro's official sample files (HERO5–8, MAX, Fusion, Karma).

## Next builds

- Phase 3: SigLIP text search ("trail jump", "summit view"), PANNs sound events, Whisper transcripts, near-duplicate folding, Apple aesthetics score.
- Phase 4: the final edit: story assembly, Gyroflow stabilization, beat-synced music, ducking and loudness, preview-then-render.
