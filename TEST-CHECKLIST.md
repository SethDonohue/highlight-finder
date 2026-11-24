# Mac test checklist (v0.1.0)

**Install**
- [ ] `brew install ffmpeg python@3.12 gh`, then `gh auth login`
- [ ] `python3.12 -m venv ~/hlf-venv && source ~/hlf-venv/bin/activate`
- [ ] `gh release download v0.1.0 -R SethDonohue/highlight-finder -p '*.whl' -D /tmp/hlf && pip install /tmp/hlf/*.whl`
- [ ] `hlf doctor`: ffmpeg, ffprobe, videotoolbox and otio all say ok

**One trip-day first**
- [ ] `hlf run --trip "Test day" --profile mtb /Volumes/<drive>/<one day folder>` finishes; note how long it took per hour of footage
- [ ] Unplug the drive mid-run, plug it back, run again: it resumes instead of starting over
- [ ] `hlf status --trip "Test day"`: GoPro clips show telemetry; chapter files became one clip

**Review**
- [ ] `hlf serve`: proposals play and loop inside their start/end
- [ ] Keep, reject, nudge start/end, rate, tag; refresh the page and it's all still there
- [ ] Junk tab: are the suggested rejects really junk? Rescue any that aren't
- [ ] `hlf serve --lan`: open the printed link on your phone, keep/reject by swiping

**Resolve**
- [ ] Export tab → Make rough cut; in Resolve: File → Import → Timeline → the .otio
- [ ] Clips link to your originals, in order, with markers; you can extend a trim past the cut
- [ ] If any 10-bit GoPro clip shows as audio-only in free Resolve, note which camera/mode

**Report back**
- [ ] Rough share of proposals you kept, and anything it missed that you remember
