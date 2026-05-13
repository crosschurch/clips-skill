# Cross Church Clips Skill

End-to-end sermon → social-clips pipeline. Takes an OBS recording (with chapter
markers) and/or a full sermon video, transcribes it, asks Claude to identify the
most viral 45–90s moments, cuts them horizontally, converts each to vertical
9:16 with AI face-tracking, and uploads the top 10 to Descript. Optionally
finalizes curated clips with background music + a Cross Church ending slate.

This repo is both:
1. **A Claude Code skill** — clone into `~/.claude/skills/sermon-clips/` and
   invoke via `/sermon-clips` in Claude Code.
2. **A standalone CLI pipeline** — clone anywhere and run the scripts directly.

---

## Pipeline

```
[OBS Recording]                    [Full Sermon Video + optional .mp3]
      │                                        │
      ├── extract_segments.py                  │ transcribe.sh (auto-detects,
      │   → marker clips (4-min windows)       │ uses faster-whisper int8 on CPU)
      │                                        │
      ├── transcribe.sh ─────────────────────→ transcripts/*.json
      │
      ├── find_moments.py ──────────────────→ viral_clips/*.mp4
      │   default mode:
      │   • 3–6 viral moments per marker clip       (45–90s each)
      │   • full sermon chunked into 4-min windows  (38+ additional clips)
      │   • horizontal highlight reel               (banger statements, 25–35s)
      │   --edited mode (opt-in):
      │   • 3–5 edited clips per marker clip        (multi-segment, fluff cut)
      │   • full sermon clips still produced
      │   → viral_clips/moments.json
      │   → viral_clips/quotes.json   (deduped standalone text quotes)
      │
      ├── make_quote_images.py ─────────────→ quote_images/quote_NN.png
      │   • 1080×1350 (Instagram 4:5) minimal, editorial quote cards
      │   • Six clean styles auto-picked per quote — all are sentence case
      │     with generous whitespace and modest type. They differ in palette
      │     and emphasis mechanic, not loudness:
      │       minimal_serif   — dark + Lora regular (default literary)
      │       soft_paper      — cream paper + Lora regular w/ one italic
      │                         emphasis word in-line (paradox / wisdom)
      │       editorial_split — charcoal + Inter, dim setup → bright payoff
      │       accent_payoff   — dark + Inter setup + italic warm-gold payoff
      │       brand_block     — deep navy + Inter regular, centered & calm
      │       scripture_card  — soft gradient + Lora italic, contemplative
      │   • find_moments.py tags each quote with {text, style, punch}; bare
      │     strings still accepted (heuristic style picker fills the gap)
      │   • Bundled fonts from skill `fonts/` dir — no install required
      │   • Flags: --attribution "Pastor Name", --style <name>, --all-styles
      │
      ├── make_sermon_recap.py ─────────────→ sermon_recap/recap.mp4
      │   • 8–12 min long-form recap of the full sermon (Furtick-style)
      │   • one Claude call picks 5–10 structural segments
      │   • hard cuts only — pauses and natural pacing stay in
      │
      ├── make_vertical.py ─────────────────→ vertical_clips/*.mp4
      │   (face-tracked 9:16 crop, ranked by virality)
      │
      ├── add_captions.py (optional) ──────→ captioned_clips/*_captioned.mp4
      │   • burns opus-style word-by-word captions onto verticals
      │   • re-transcribes each clip with whisper (base.en by default) so
      │     timestamps are clip-relative
      │   • presets: opus (white + yellow active word, default), karaoke
      │     (green active word, 4-word chunks), minimal (no highlight)
      │   • uses bundled Inter Black / Inter Regular via libass fontsdir
      │   • opt-in; skip if you're going to caption in Descript instead
      │
      ├── upload_to_descript.py ───────────→ Descript ▸ Cross Church ▸ <Session>
      │   • default: top N verticals by virality
      │   • --edited mode: auto-uploads all edited verticals instead
      │   • token: ~/.config/sermon-clips/descript.env
      │
      │   [Manual: review in Descript, copy keepers into edited_clips/]
      │
      └── finalize_clips.py ───────────────→ final_clips/*.mp4
          • background music at -18 dB, ramps to full at outro
          • Cross Church ending slate appended (1s xfade)
```

---

## Prerequisites

### System binaries

| Binary  | Install (macOS) | Install (Linux) |
|---------|-----------------|-----------------|
| `ffmpeg` (with `ffprobe`) | `brew install ffmpeg` | `apt install ffmpeg` |
| `whisper` (OpenAI CLI) | `pipx install openai-whisper` | `pipx install openai-whisper` |
| `claude` (Claude Code CLI) | https://docs.claude.com/claude-code | https://docs.claude.com/claude-code |

`make_vertical.py` is written against the system Python (`/usr/bin/python3`,
3.9+) because OpenCV + the system video stack is more reliable there than in a
virtualenv on macOS. Other scripts are happy with any Python 3.9+.

### Python packages

```bash
/usr/bin/python3 -m pip install --user -r requirements.txt
```

The `faster-whisper` line is optional — `transcribe.sh` falls back to plain
`whisper` if it isn't importable. With `faster-whisper` a 36-min sermon
transcribes in ~10–15 min on CPU; without it, plan on ~45 min.

### Claude Code CLI

`find_moments.py` shells out to `claude -p "<prompt>"` to pick viral moments,
so the `claude` CLI must be on `$PATH` and authenticated. No prompt files,
no MCP servers — just `claude -p`. This script will spend Claude credits.

### Descript token (optional, only for upload step)

Create the file:

```
~/.config/sermon-clips/descript.env
```

with one line:

```
DESCRIPT_API_TOKEN=dx_bearer_<id>:dx_secret_<secret>
```

Generate the token in Descript ▸ Settings ▸ Advanced ▸ API. The pipeline uses
the team API import endpoint and requires `team_access` to be set; the script
sets `team_access: "edit"` automatically.

### Music + ending slate (only for `finalize_clips.py`)

`finalize_clips.py` needs an assets directory containing:

```
<assets>/endings/cross_church_ending.mp4
<assets>/music/*.mp3            (any number of tracks; one shuffled per clip)
```

Resolution order:
1. `$SERMON_CLIPS_ASSETS_DIR` if set
2. `<repo>/assets/` if it exists
3. `~/Code/crosschurch-new/clipsy/assets` (legacy)

Assets are **not** in this repo — `.gitignore` excludes the `assets/`
directory because the music tracks are licensed and the ending slate is large.
Distribute them out of band.

---

## Installation

### As a Claude Code skill (recommended for Cross Church use)

```bash
mkdir -p ~/.claude/skills
git clone https://github.com/crosschurch/clips-skill.git ~/.claude/skills/sermon-clips
```

In Claude Code, the `/sermon-clips` skill becomes available immediately. Run it
from the sermon's working directory:

```
/sermon-clips                              # use CWD
/sermon-clips path/to/sermon.mp4           # full sermon video
/sermon-clips path/to/clips_directory/     # directory with marker clips
```

### As a standalone CLI

```bash
git clone https://github.com/crosschurch/clips-skill.git
cd clips-skill
/usr/bin/python3 -m pip install --user -r requirements.txt
```

Then call the scripts directly (see "Running it manually" below).

---

## Running it manually

All scripts are idempotent and skip work that's already done — re-running after
a partial failure is safe.

```bash
cd /path/to/sermon_working_dir/

# 1. (optional) Extract 4-min marker clips around OBS chapter markers
python3 /path/to/clips-skill/scripts/extract_segments.py "raw_sermon.mp4"

# 2. Transcribe marker clips + the full sermon
bash /path/to/clips-skill/scripts/transcribe.sh

# 3. Find viral moments → cut horizontal clips into viral_clips/
python3 /path/to/clips-skill/scripts/find_moments.py
#   add --edited for multi-segment "fluff cut" clips (skips simple per-marker
#   cuts; the edited verticals become the Descript upload set in step 5)
#   Also writes viral_clips/quotes.json (deduped standalone text quotes).

# 3b. Render quote cards from quotes.json → quote_images/
python3 /path/to/clips-skill/scripts/make_quote_images.py
#   optional: --attribution "Pastor Name"

# 3c. Build the long-form 8–12 min recap → sermon_recap/recap.mp4
python3 /path/to/clips-skill/scripts/make_sermon_recap.py
#   optional: --target-minutes 11

# 4. Convert to vertical 9:16 (face-tracked crop) → vertical_clips/
/usr/bin/python3 /path/to/clips-skill/scripts/make_vertical.py

# 4b. (optional) Burn opus-style captions → captioned_clips/
python3 /path/to/clips-skill/scripts/add_captions.py
#   default style is "opus" (big bold white + yellow active word).
#   Skip this step if you'd rather caption in Descript / CapCut.

# 5. Upload top 10 to Descript (one project per clip, in "Cross Church" folder)
python3 /path/to/clips-skill/scripts/upload_to_descript.py

# --- manual step: review in Descript, copy keepers into edited_clips/ ---

# 6. Finalize curated clips with music + ending slate → final_clips/
python3 /path/to/clips-skill/scripts/finalize_clips.py
```

### Useful flags

| Script | Flags |
|--------|-------|
| `find_moments.py` | `--edited` — multi-segment edited clips (skips simple per-marker cuts) |
| `make_quote_images.py` | `--attribution "Name"`, `--style <name>` (force one style for every quote), `--all-styles` (render each quote in every style) |
| `make_sermon_recap.py` | `--target-minutes N` — override default 10 min target |
| `add_captions.py` | `--style opus|karaoke|minimal`, `--model tiny.en|base.en|small.en`, `--out-dir <name>`, `--inplace` (overwrite verticals), or pass specific .mp4 paths to caption just those |
| `upload_to_descript.py` | `--top N`, `--all`, `--folder <name>`, `--session <name>`, `--skip N` (resume after partial failure), `--wait` (block on Descript processing; slow), `--dry-run`. Auto-detects edited mode from `vertical_clips/edited_*` files. |
| `make_vertical.py` | Pass a single clip path to process just that file |

---

## What goes into each working directory

A typical sermon directory looks like this after a full run:

```
sermon_0503/
├── raw_sermon.mp4                       # the OBS recording
├── *_marker_*.mp4                       # 4-min windows around markers
├── transcripts/                         # whisper JSON transcripts
│   └── *.json
├── viral_clips/                         # horizontal cuts
│   ├── moments.json                     # virality scores + pick metadata
│   ├── quotes.json                      # deduped standalone text quotes
│   ├── *.mp4                            # ~45 clips (markers + full + edited)
│   └── highlight_reel.mp4               # standalone banger statements
├── quote_images/                        # 1080×1350 styled quote cards
│   └── quote_NN.png + quote_NN.txt
├── sermon_recap/                        # 8–12 min long-form recap
│   ├── recap.mp4
│   └── manifest.json
├── vertical_clips/                      # 9:16 face-tracked
│   └── *_vertical.mp4
├── captioned_clips/                     # optional, only if add_captions.py was run
│   └── *_captioned.mp4
├── edited_clips/                        # YOU create this — keepers from Descript
│   └── *.mp4
├── final_clips/                         # music + ending slate
│   └── *.mp4
└── descript_uploads.json                # manifest from upload step
```

---

## Tuning

Defaults in `find_moments.py`:

| Setting | Default | Notes |
|---------|---------|-------|
| Whisper model | `medium` | `small` is faster but less accurate |
| Clip length | 45–90s | Hard reject under 40s or over 95s |
| Moments per marker clip | 3–6 | Claude picks fewer if material is thin |
| `CLIP_BUFFER` | 6.0s | Editing headroom on each side of every cut |
| `EDITED_SEG_HEAD_PAD` | 0.15s | Lead-in pad on each segment of multi-segment edited clips |
| `EDITED_SEG_TAIL_PAD` | 0.40s | Trailing pad — Whisper timestamps land before the final consonant |
| Vertical output | 1080×1920 | lanczos-upscaled from a 9:16 slice |
| Face-tracking sigma | 1.5s | Higher = smoother but slower to follow movement |
| H.264 CRF | 18 | Lower = better quality, larger file |

`finalize_clips.py`:

| Setting | Default |
|---------|---------|
| `MUSIC_BG_DB` | -18 dB |
| `XFADE_DUR` | 1.0s |
| H.264 encoder | `h264_videotoolbox` (macOS hardware) — change for Linux |

---

## Automation tips

The pipeline is mostly hands-off but **two steps are not automatable**:

1. **Curating `edited_clips/`** — picking which clips are actually worth
   posting is judgment work. The current flow uploads top 10 to Descript so the
   editor can review/trim there, then copies keepers locally.
2. **Adding captions** — none of the scripts produce captioned output.
   Captions are added in CapCut/DaVinci/Descript before posting to socials.

For everything else, you can wire up the steps in a shell script:

```bash
#!/bin/bash
set -euo pipefail
cd "$1"
python3 ~/clips-skill/scripts/extract_segments.py "$(ls *.mp4 | grep -v _marker_ | head -1)"
bash ~/clips-skill/scripts/transcribe.sh
python3 ~/clips-skill/scripts/find_moments.py
/usr/bin/python3 ~/clips-skill/scripts/make_vertical.py
python3 ~/clips-skill/scripts/upload_to_descript.py
```

---

## Troubleshooting

**No faces detected** → Tracking falls back to center crop. Still functional if
the pastor stays roughly center-frame.

**Claude returns bad JSON** → `find_moments.py` prints the raw output. Usually
a retry fixes it; occasionally a transcript is too long and you need to bump
the `timeout=300` in the script.

**Audio out of sync after vertical** → Should not happen with the current
pipe-based encoder. If it does, check that the source clip's fps reported by
ffprobe matches what OpenCV reads (`cap.get(cv2.CAP_PROP_FPS)`).

**Whisper FP16 warning** → Normal on CPU-only machines. Continues with FP32.

**Marker clips have same timestamp** → OBS bug — multiple markers can land on
the same frame. The clips will overlap; Claude will still find the best moments.

**Descript upload returns HTTP 400 about `team_access`** → The Descript API
now requires `team_access` when `folder_name` is set. The script already sends
`"team_access": "edit"` — if you forked, make sure that field is present.

**Descript upload looks hung** → If the script is idling at 0% CPU with no
network sockets, it's a macOS `_scproxy.get_proxies()` hang against
`com.apple.netsrc`. The script installs an empty `ProxyHandler` at import
time to bypass it. If you forked an old version, set `no_proxy='*'` in the
environment as a workaround.

**Upload taking 10 min per clip** → Old versions polled each Descript job
until terminal status (which the job never reached). Polling is now opt-in
via `--wait`; the default is fire-and-forget. The project URL is valid the
moment the import POST returns — Descript finishes processing in the
background. Resume a partial run with `--skip N`.

**`h264_videotoolbox: Function not implemented`** → You're on Linux. Change the
encoder in `finalize_clips.py` to `libx264 -crf 20 -preset medium`.

---

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | Claude Code skill manifest — agent instructions for `/sermon-clips` |
| `scripts/extract_segments.py` | Cut 4-min windows around OBS chapter markers |
| `scripts/transcribe.sh` | Whisper transcription for marker clips + full sermon |
| `scripts/transcribe_faster.py` | faster-whisper helper invoked by `transcribe.sh` |
| `scripts/find_moments.py` | Pick viral moments + quotes via Claude, cut horizontal clips |
| `scripts/make_quote_images.py` | Render 1080×1350 styled quote cards from `viral_clips/quotes.json` (six styles, auto-picked per quote) |
| `scripts/add_captions.py` | Burn opus-style word-by-word captions onto vertical clips (adapted from `clipify`'s build_ass.py — MIT) |
| `fonts/` | Bundled Google Fonts (Anton, Bebas Neue, Inter, Lora, Permanent Marker, Yellowtail, Alfa Slab One) used by the quote-card styles |
| `style_refs/` | Reference PNGs that inspired the quote-card styles (committed for design intent — not consumed at runtime) |
| `scripts/make_sermon_recap.py` | Build the 8–12 min long-form recap of the full sermon |
| `scripts/make_vertical.py` | OpenCV face-tracked 9:16 conversion |
| `scripts/upload_to_descript.py` | Upload top N verticals to Descript |
| `scripts/finalize_clips.py` | Add music + ending slate to curated clips |
| `requirements.txt` | Python deps for the pipeline |
| `.gitignore` | Excludes media + cache + assets |

---

## License

Internal Cross Church tooling. No license granted for external use.
