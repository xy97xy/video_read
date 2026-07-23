# Issues

## [2026-07-21] torchcodec fails to seek HEVC (hvc1) files

**File:** `output/takeout/Takeout/Google Photos/Photos from 2026/My_Edit_Render.mp4`

**Symptom:** torchcodec throws `Could not seek file to pts=0: Invalid argument` on every scene segment. torchvision fallback also fails (`module 'torchvision.io' has no attribute 'read_video'`). Worker retried all 55+ scenes before dying.

**Root cause:** torchcodec bug with HEVC (`hvc1`) encoded via `Lavf59.27.100`. File is valid — ffprobe reads it fine (4096×2304, 59.94fps, 6 min, ~32Mbps).

**Fix attempted:** Remux with `ffmpeg -c copy` to fix container seek tables → retest with torchcodec.

**Longer-term fix needed:**
- Add ffmpeg-frame-extraction fallback for files where torchcodec + torchvision both fail
- Or add per-file retry limit so one bad file can't stall the whole worker
