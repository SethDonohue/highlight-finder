# Mac test checklist (v0.2.0)

**Install (one line)**
- [ ] Paste in Terminal: `curl -fsSL https://raw.githubusercontent.com/SethDonohue/highlight-finder/main/install.sh | bash`
- [ ] It ends with "Done" and `doctor` shows ffmpeg, ffprobe, videotoolbox and otio as ok
- [ ] Open a new Terminal window, type `hlf`: the menu appears
- [ ] Paste the install line again: it finishes quickly (that's the update path)

**Guided new trip**
- [ ] 1) Start a new trip. Drag a one-day folder into the window: it shows how many videos (GoPro vs phone), hours and GB
- [ ] Try Enter on the folder question (says it's required), `?` (more help) and `b` (goes back)
- [ ] Trip name default makes sense (not "DCIM" or "100GOPRO"); activity accepts a number or a name
- [ ] The summary shows free space and a `hlf run ...` command; Enter starts it

**While it runs**
- [ ] Progress lines update in place; time left appears after a few clips
- [ ] Press Ctrl+C mid-run: it says Paused. Run `hlf` again: the menu shows "x of y clips read"; Continue a trip finishes it
- [ ] Unplug the drive, then Continue a trip: it says the videos can't be found and offers to check again or point to them

**Next steps**
- [ ] Review on this Mac opens your browser; Ctrl+C in Terminal comes back to the menu
- [ ] Review on your phone prints a link that works on your phone (same Wi-Fi)
- [ ] Rough cut before keeping anything: it tells you to review first. After keeping some: makes the .otio and offers to show it in Finder
- [ ] In Resolve: File > Import > Timeline > the .otio; clips link to originals with markers
- [ ] Final cut shows "coming in a later version"

**Report back**
- [ ] Any question that was confusing, a default that was wrong, or a step that felt slow
- [ ] Rough share of picks you kept, and anything it missed
