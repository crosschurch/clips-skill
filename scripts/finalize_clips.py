#!/usr/bin/env python3
"""
Finalize captioned vertical clips by adding background music and the Cross Church
ending slate. For each clip:

  1. Scale the ending clip to match the sermon clip resolution
  2. xfade the ending over the last XFADE_DUR seconds (ending fades in)
  3. Layer background music at MUSIC_BG_DB under the sermon audio
  4. Fade music up to 0 dB as the ending fades in, fade sermon audio out
  5. Output to final_clips/

Usage:
    python3 finalize_clips.py [work_dir]
    work_dir defaults to CWD — expects an edited_clips/ subdirectory

Assets dir resolution order:
  1. $SERMON_CLIPS_ASSETS_DIR (if set)
  2. <repo>/assets (sibling of this script's parent — works when the
     repo is cloned anywhere)
  3. ~/Code/crosschurch-new/clipsy/assets (legacy default)
The chosen dir must contain endings/cross_church_ending.mp4 and music/*.mp3.
"""

import json
import os
import random
import subprocess
import sys
import glob

WORK_DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
EDITED_DIR = os.path.join(WORK_DIR, "edited_clips")
FINAL_DIR = os.path.join(WORK_DIR, "final_clips")


def _resolve_assets_dir():
    env = os.environ.get("SERMON_CLIPS_ASSETS_DIR")
    if env:
        return os.path.expanduser(env)
    repo_assets = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
    )
    if os.path.isdir(repo_assets):
        return repo_assets
    return os.path.expanduser("~/Code/crosschurch-new/clipsy/assets")


ASSETS_DIR = _resolve_assets_dir()
ENDINGS_DIR = os.path.join(ASSETS_DIR, "endings")
MUSIC_DIR = os.path.join(ASSETS_DIR, "music")

ENDING_FILE = os.path.join(ENDINGS_DIR, "cross_church_ending.mp4")
OUTPUT_W, OUTPUT_H = 1080, 1920  # Final output resolution (1080p vertical)
XFADE_DUR = 1.0         # seconds of crossfade into ending
MUSIC_BG_DB = -18        # background music level during sermon
MUSIC_BG_VOL = 10 ** (MUSIC_BG_DB / 20)  # ≈ 0.126


def get_duration(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def get_resolution(path):
    cmd = [
        "ffprobe", "-v", "quiet", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def get_music_tracks():
    tracks = sorted(glob.glob(os.path.join(MUSIC_DIR, "*.mp3")))
    if not tracks:
        print("✗ No music tracks found in:", MUSIC_DIR)
        sys.exit(1)
    return tracks


def finalize_clip(clip_path, output_path, ending_path, music_path):
    """Add background music + ending slate to a single captioned clip."""
    clip_dur = get_duration(clip_path)
    ending_dur = get_duration(ending_path)
    # xfade offset: where the crossfade begins
    xfade_offset = clip_dur - XFADE_DUR
    # Total output duration
    total_dur = clip_dur + ending_dur - XFADE_DUR

    # Music volume envelope (evaluated per-frame):
    #   0 → xfade_offset:            MUSIC_BG_VOL  (background)
    #   xfade_offset → xfade_offset + XFADE_DUR:   ramp MUSIC_BG_VOL → 1.0
    #   xfade_offset + XFADE_DUR → end:            1.0  (full volume)
    vol_expr = (
        f"if(lt(t,{xfade_offset:.2f}),"
        f"{MUSIC_BG_VOL:.4f},"
        f"if(lt(t,{xfade_offset + XFADE_DUR:.2f}),"
        f"{MUSIC_BG_VOL:.4f}+({1.0 - MUSIC_BG_VOL:.4f})*(t-{xfade_offset:.2f})/{XFADE_DUR:.2f},"
        f"1.0))"
    )

    # Sermon audio fade-out: ramp from 1.0 → 0.0 over the crossfade
    sermon_fade_expr = (
        f"if(lt(t,{xfade_offset:.2f}),1.0,"
        f"if(lt(t,{xfade_offset + XFADE_DUR:.2f}),"
        f"1.0-(t-{xfade_offset:.2f})/{XFADE_DUR:.2f},"
        f"0.0))"
    )

    w, h = OUTPUT_W, OUTPUT_H

    filter_complex = (
        # Scale sermon clip to output resolution, normalise fps/format for xfade
        f"[0:v]scale={w}:{h},fps=30,format=yuv420p[main_v];"

        # Scale ending to output resolution
        f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p,setsar=1[ending_v];"

        # Video: xfade transition
        f"[main_v][ending_v]xfade=transition=fade:duration={XFADE_DUR}:offset={xfade_offset:.2f}[outv];"

        # Music: loop to cover total duration, apply volume envelope
        f"[2:a]aloop=loop=-1:size=2e+09,atrim=0:{total_dur:.2f},"
        f"volume='{vol_expr}':eval=frame[music];"

        # Sermon audio: apply fade-out, pad to total duration
        f"[0:a]volume='{sermon_fade_expr}':eval=frame,"
        f"apad=whole_dur={total_dur:.2f}[sermon_a];"

        # Mix sermon audio + music (no auto-normalization)
        f"[sermon_a][music]amix=inputs=2:duration=first:normalize=0[outa]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", clip_path,        # 0: sermon clip
        "-i", ending_path,      # 1: ending slate
        "-i", music_path,       # 2: background music
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "h264_videotoolbox", "-b:v", "8M", "-allow_sw", "1",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    ✗ ffmpeg error: {result.stderr[-300:]}")
        return False
    return True


def main():
    if not os.path.isdir(EDITED_DIR):
        print(f"No edited_clips/ directory found in: {WORK_DIR}")
        sys.exit(1)

    os.makedirs(FINAL_DIR, exist_ok=True)

    clips = sorted(glob.glob(os.path.join(EDITED_DIR, "*.mp4")))
    if not clips:
        print(f"No MP4 files in: {EDITED_DIR}")
        sys.exit(1)

    music_tracks = get_music_tracks()

    print(f"Finalizing {len(clips)} clip(s)")
    print(f"  Source:  {EDITED_DIR}")
    print(f"  Output:  {FINAL_DIR}")
    print(f"  Ending:  {os.path.basename(ENDING_FILE)}")
    print(f"  Music:   {len(music_tracks)} tracks available")
    print()

    # Shuffle music tracks so each clip gets a unique song (cycles if more clips than tracks)
    random.shuffle(music_tracks)

    success = 0
    for idx, clip_path in enumerate(clips):
        name = os.path.basename(clip_path)
        out_path = os.path.join(FINAL_DIR, name)

        if os.path.exists(out_path):
            print(f"  ✓ Already done: {name}")
            success += 1
            continue

        music = music_tracks[idx % len(music_tracks)]
        music_name = os.path.splitext(os.path.basename(music))[0]

        print(f"  ▶ {name}")
        print(f"    music: {music_name}")

        if finalize_clip(clip_path, out_path, ENDING_FILE, music):
            size_mb = os.path.getsize(out_path) / 1_000_000
            dur = get_duration(out_path)
            print(f"    ✓ {dur:.0f}s, {size_mb:.1f} MB")
            success += 1
        else:
            print(f"    ✗ Failed")

    print(f"\n{'='*50}")
    print(f"✓ {success}/{len(clips)} clips finalized → {FINAL_DIR}")


if __name__ == "__main__":
    main()
