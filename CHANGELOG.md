# Changelog

## v0.2.1 (2026-11-24)
- Guided mode menus use the arrow keys: up/down to move, Enter to choose. Also for the activity, the trip picker and Yes/No. A number still jumps straight to an option; q quits, b or Esc goes back where you can.
- Options that aren't available yet are greyed out and skipped.
- Folder and trip name are still typed (drag-and-drop works). Without a real terminal, menus fall back to typing a number (or set HLF_PLAIN=1).

## v0.2.0 (2026-11-24)
- Guided mode: run `hlf` alone for a menu that walks you through a trip. Each question says if it's required or optional, why, and its default; `?` help, `b` back, `q` quit. Drag-and-drop folders work.
- Progress while it runs: a line per step with a bar, count, time left and the current file. Ctrl+C pauses safely; Continue a trip resumes.
- Next steps when done: review on the Mac or phone, Resolve rough cut (only after you keep picks), add footage. Final cut is listed as coming later.
- Finds footage again if a drive was unplugged or renamed.
- One-line installer (`install.sh`) that also updates.

## v0.1.0 (2026-11-24)
- Ingest GoPro and phone footage; stitch GoPro chapter files; use `.LRV` proxies when present.
- GoPro telemetry straight from the file: motion, airtime, GPS speed, HiLight tags.
- One decode pass per clip (VideoToolbox on the Mac): review proxy, frames, audio, motion/exposure/sharpness/loudness signals.
- Junk triage and highlight proposals with a "why" line; dead stretches skipped, never deleted.
- Review UI for Mac and phone; DaVinci Resolve rough cut (.otio) with markers; lossless clip export.
- Settings with a tuning log; `frames` and `note` commands for Claude's editorial pass.
