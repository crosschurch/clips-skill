---
name: sermon-clips
description: End-to-end sermon video pipeline — like Opus Clips but tuned for Cross Church sermons. Takes an OBS recording (with chapter markers) AND/OR a full sermon video, transcribes with Whisper/faster-whisper, picks the most viral 45-70s moments using Claude, cuts them (including tightly-edited multi-segment clips), then converts each to vertical 9:16 with AI face-tracking. Also builds a horizontal highlight reel of standalone banger statements. Finalizes clips with background music and Cross Church ending slate. Use when asked to "make clips", "create sermon clips", "process sermon video", "make vertical clips", "finalize clips", or any variation of extracting social content from a sermon recording.
---

# Sermon Clips Pipeline

Full pipeline: OBS recording → transcription → viral moment selection → horizontal cuts → vertical 9:16 with subject tracking → music + ending slate.

## Prerequisites

- `whisper` CLI installed (`pipx install openai-whisper`)
- `ffmpeg` installed
- `/usr/bin/python3` (system Python 3.9) with: `cv2`, `numpy`, `scipy`
- Clips directory from `extract_segments.py` or pre-cut segments

## Usage

```
/sermon-clips [path/to/video.mp4 | path/to/clips/directory]
```

- No argument → use current working directory
- Video file argument → run full pipeline from the raw recording
- Directory argument → find existing marker clips and continue from there

---

## Pipeline Overview

```
[OBS Recording]                    [Full Sermon Video + optional .mp3]
      │                                        │
      ├── extract_segments.py                  │ transcribe.sh (auto-detects,
      │   → marker clips (4-min windows)       │ uses faster-whisper int8 on CPU)
      │                                        │
      ├── transcribe.sh ─────────────────────→ transcripts/*.json
      │   (marker clips + full sermon)
      │
      ├── find_moments.py ──────────────────→ viral_clips/*.mp4
      │   • 3-6 viral moments per marker clip       (45-70s each)
      │   • 3-5 edited clips per marker clip        (multi-segment, fluff cut)
      │   • full sermon chunked into 4-min windows  (38+ additional clips)
      │   • horizontal highlight reel               (banger statements, 25-35s)
      │   → viral_clips/moments.json
      │
      ├── make_vertical.py ─────────────────→ vertical_clips/*.mp4
      │   (face-tracked 9:16 crop, ranked by virality)
      │
      ├── upload_to_descript.py ───────────→ Descript ▸ Cross Church ▸ <Session>
      │   • top 10 verticals as compositions in one new project
      │   • token: ~/.config/sermon-clips/descript.env ($DESCRIPT_API_TOKEN)
      │
      │   [Manual step: review clips, curate keepers into edited_clips/]
      │
      └── finalize_clips.py ───────────────→ final_clips/*.mp4
          • background music layered under sermon audio (-18 dB)
          • music ramps to full volume as ending fades in
          • sermon audio fades out over crossfade
          • Cross Church ending slate appended (1s xfade)
          Assets: ~/Code/crosschurch-new/clipsy/assets/
            endings/cross_church_ending.mp4
            music/*.mp3  (10 tracks, shuffled per clip)
```

---

## Step-by-Step Instructions for Claude

### 1. Determine working directory and source

- If a `.mp4` file is passed: set `WORK_DIR` to its parent directory, `SOURCE_VIDEO` to that file
- If a directory is passed or no argument: set `WORK_DIR` to that directory (or CWD)
- Check for existing marker clips: `ls "$WORK_DIR"/*_marker_*.mp4`
- Check for existing transcripts: `ls "$WORK_DIR/transcripts"/*.json`
- Check for existing viral clips: `ls "$WORK_DIR/viral_clips"/*.mp4`

Report what was found and what steps remain.

### 2. Extract marker segments (if needed)

If no marker clips exist but a source `.mp4` exists, run:

```bash
cd "$WORK_DIR" && python3 extract_segments.py "$(basename $SOURCE_VIDEO)"
```

This produces `*_marker_*.mp4` files (4-minute windows around OBS chapter markers).

### 3. Transcribe clips

Run in the working directory:

```bash
cd "$WORK_DIR" && bash ~/.claude/skills/sermon-clips/scripts/transcribe.sh
```

This transcribes **both** the marker clips and the full sermon video:
- Marker clips: standard `whisper` CLI (medium model)
- Full sermon: auto-detects any non-marker MP4 >10min, prefers a matching `.mp3`/`.m4a` audio file (same duration ±120s) for speed, uses `faster-whisper` (int8 CPU mode, ~4x faster than standard whisper)

Outputs: `transcripts/*.json` — skips already-done files.

Note: ~5-6 min per 4-minute marker clip; ~10-15 min for a 36-min full sermon with faster-whisper.

### 4. Find viral moments and cut clips

```bash
cd "$WORK_DIR" && python3 ~/.claude/skills/sermon-clips/scripts/find_moments.py
```

This:
- Reads each transcript
- Sends to Claude CLI to identify 3-6 best viral moments (45-70s each)
- Cuts each moment from the source marker clip with ffmpeg
- Saves to `viral_clips/` with a `moments.json` manifest

Check `viral_clips/moments.json` to review titles and timings.

### 5. Convert to vertical 9:16

```bash
cd "$WORK_DIR" && /usr/bin/python3 ~/.claude/skills/sermon-clips/scripts/make_vertical.py
```

This:
- Loads each clip from `viral_clips/`
- Samples ~4 frames/sec for face/upper-body detection
- Builds a Gaussian-smoothed crop trajectory (1.5s sigma)
- Processes all frames with dynamic 9:16 crop
- Re-encodes to H.264 CRF 18
- Saves to `vertical_clips/`

### 6. Upload top 10 verticals to Descript

```bash
cd "$WORK_DIR" && python3 ~/.claude/skills/sermon-clips/scripts/upload_to_descript.py
```

This:
- Reads `viral_clips/moments.json`, picks the top 10 by `virality_total`
- Matches each to `vertical_clips/<vertical_file>`
- Creates **one new Descript project** named after the working directory
  (e.g. "Sermon 0419") inside the **Cross Church** folder, with one composition per clip
- Uses the token at `~/.config/sermon-clips/descript.env`
  (set `DESCRIPT_API_TOKEN=dx_bearer_...:dx_secret_...`)

Flags:
- `--top N` — upload top N instead of 10
- `--all` — upload every moment in moments.json
- `--folder "Other"` / `--project "Custom"` — override destination
- `--dry-run` — print the payload without calling the API

Note: Descript's import API always creates a new project, so re-running this
step creates a duplicate project. Skip the step if you only want to retry
downstream work.

### 7. Curate into edited_clips/

After Descript upload, the user manually reviews clips and moves keepers into `edited_clips/`. Wait for this step before proceeding.

### 8. Finalize with music + ending slate

```bash
cd "$WORK_DIR" && python3 ~/.claude/skills/sermon-clips/scripts/finalize_clips.py
```

This:
- Takes each clip from `edited_clips/`
- Layers background music at -18 dB under the sermon audio
- Crossfades the Cross Church ending slate over the last 1 second
- Ramps music to full volume as the ending fades in, sermon audio fades out
- Re-encodes with VideoToolbox (hardware accelerated) at 8 Mbps
- Saves to `final_clips/`

Assets required: `~/Code/crosschurch-new/clipsy/assets/endings/cross_church_ending.mp4` and `music/*.mp3`

### 9. Report results

After pipeline completes:
- List all clips in `final_clips/` with their titles and durations
- Note any clips that failed or were skipped
- Remind user: no captions yet — add those in CapCut/DaVinci before posting

---

## Selective Re-running

Each step is idempotent and skips already-completed work:
- Transcription skips clips with existing `.json` files
- Vertical conversion skips clips with existing `_vertical.mp4` files

To re-process a specific clip only:
```bash
/usr/bin/python3 ~/.claude/skills/sermon-clips/scripts/make_vertical.py "path/to/clip.mp4"
```

---

## Configuration / Tuning

These are the defaults — adjust per-session if the user asks:

| Setting | Default | Notes |
|---------|---------|-------|
| Whisper model | `medium` | `small` is faster but less accurate |
| Clip length | 45–70s | Hard limits: reject <35s or >80s |
| Moments per clip | 3–6 | Claude will pick fewer if clip has fewer good moments |
| Vertical output | 1080×1920 (9:16) | lanczos-upscaled from a 9:16 slice of the 1080p source |
| Face tracking sigma | 1.5s | Higher = smoother but slower to follow fast movement |
| H.264 CRF | 18 | Lower = better quality, larger file |

---

## Troubleshooting

**No faces detected** → Tracking falls back to center crop. The pastor may be in wide shot, sitting, or looking away. Still functional.

**Claude returns bad JSON** → `find_moments.py` will print the raw response. Usually a retry works.

**Audio out of sync** → Should not happen with the current pipe-based encoder. If it does, check that the source clip's fps reported by ffprobe matches what OpenCV reads (`cap.get(cv2.CAP_PROP_FPS)`). The fix is already baked in: `make_vertical.py` pipes raw frames directly into ffmpeg and pulls audio from the original file in one pass — no temp files, no drift.

**Whisper FP16 warning** → Normal on CPU-only machines. Processing continues with FP32 (slower but correct).

**Marker clips have same timestamp** → This is an OBS bug where multiple markers land on the same frame. The clips will have overlapping content — Claude will still find the best moments from each.

**`claude -p` timeout** → The default timeout is 300s. If a transcript is very long or the model is slow, increase `timeout=300` in `find_moments.py`.
