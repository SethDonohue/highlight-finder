# Changelog

## v0.1.0 (2026-11-24)
- Ingest GoPro and phone footage; stitch GoPro chapter files; use `.LRV` proxies when present.
- GoPro telemetry straight from the file: motion, airtime, GPS speed, HiLight tags.
- One decode pass per clip (VideoToolbox on the Mac): review proxy, frames, audio, motion/exposure/sharpness/loudness signals.
- Junk triage and highlight proposals with a "why" line; dead stretches skipped, never deleted.
- Review UI for Mac and phone; DaVinci Resolve rough cut (.otio) with markers; lossless clip export.
- Settings with a tuning log; `frames` and `note` commands for Claude's editorial pass.
