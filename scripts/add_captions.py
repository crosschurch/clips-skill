#!/usr/bin/env python3
"""
Burn opus-style word-by-word captions onto vertical clips.

Adapted from clipify/build_ass.py (MIT, Louise de Sadeleer / Tella) for
sermon clip needs:
  - Renders to 1080×1920 (our vertical canvas)
  - Uses bundled Inter Black / Inter Regular via libass fontsdir
  - Positioned ~22% from bottom — clear of the pastor's face but visible

Three presets:
  opus    — big bold Inter Black + yellow active word (default)
  karaoke — 4-word chunks, green active word
  minimal — Inter Regular, no highlight, smaller

Usage:
    python3 add_captions.py [work_dir | clip1.mp4 clip2.mp4 ...]
                             [--style opus|karaoke|minimal]
                             [--model tiny.en|base.en|small.en]
                             [--out-dir captioned_clips]
                             [--inplace]   (overwrite vertical_clips/<name>.mp4)

    No args     → process every *.mp4 in <cwd>/vertical_clips/
    Dir arg     → process that dir's vertical_clips/
    .mp4 args   → process those specific files

Outputs to <work_dir>/captioned_clips/<name>.mp4 by default. Skips clips
that already have a captioned counterpart.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(SKILL_ROOT, "fonts")

PLAY_W, PLAY_H = 1080, 1920
HW_ACCEL = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []

# ASS colors are AABBGGRR in hex (yes, reversed). Alpha 00 = opaque.
WHITE = "&H00FFFFFF&"
BLACK_OUTLINE = "&H00000000&"
YELLOW = "&H0000FFFF&"      # active word in opus
GREEN = "&H0000FF00&"       # active word in karaoke

# Inter Black for big-caption styles; Inter Regular for minimal.
# These come from libass via fontsdir (our skill's fonts/ dir).
PRESETS = {
    "opus": dict(
        font="Inter Black",
        size=92,
        chunk=3,
        highlight=YELLOW,
        outline=8,
        shadow=2,
        margin_v=420,
        alignment=2,            # bottom-center
    ),
    "karaoke": dict(
        font="Inter Black",
        size=88,
        chunk=4,
        highlight=GREEN,
        outline=6,
        shadow=2,
        margin_v=420,
        alignment=2,
    ),
    "minimal": dict(
        font="Inter",
        size=68,
        chunk=5,
        highlight=None,
        outline=4,
        shadow=1,
        margin_v=0,            # ignored when alignment is middle (4-6)
        alignment=5,           # middle-center (vertically + horizontally centered)
    ),
}


def fmt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def transcribe_clip(clip_path, model_name, work_dir):
    """Run whisper on the clip to get word-level timestamps relative to clip start.

    Prefers faster-whisper (int8 CPU) when available; falls back to the
    standard whisper CLI. Returns the path to the resulting *.json.
    """
    stem = os.path.splitext(os.path.basename(clip_path))[0]
    json_path = os.path.join(work_dir, f"{stem}.json")

    # Try faster-whisper first via our existing helper script
    fw_helper = os.path.join(os.path.dirname(__file__), "transcribe_faster.py")
    try:
        import importlib
        importlib.import_module("faster_whisper")
        if os.path.exists(fw_helper):
            r = subprocess.run(
                ["python3", fw_helper, clip_path, work_dir, stem],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and os.path.exists(json_path):
                return json_path
    except ImportError:
        pass

    # Fallback: whisper CLI
    r = subprocess.run(
        [
            "whisper", clip_path,
            "--model", model_name,
            "--language", "en",
            "--output_format", "json",
            "--output_dir", work_dir,
            "--word_timestamps", "True",
            "--verbose", "False",
        ],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"   ✗ whisper failed:\n{r.stderr[-400:]}")
        return None
    return json_path if os.path.exists(json_path) else None


def load_words(whisper_json):
    """Extract a flat list of {start, end, text} word records."""
    with open(whisper_json) as f:
        data = json.load(f)
    segments = data.get("segments") or data
    if not isinstance(segments, list):
        return []

    words = []
    for seg in segments:
        for w in seg.get("words", []) or []:
            text = (w.get("word") or "").strip()
            if not text:
                continue
            try:
                start = float(w["start"])
                end = float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                end = start + 0.05
            words.append({"start": start, "end": end, "text": text})
    return words


def build_ass(words, ass_path, style="opus"):
    """Build an ASS subtitle file with active-word highlighting per preset."""
    p = PRESETS.get(style, PRESETS["opus"])

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {PLAY_W}\n"
        f"PlayResY: {PLAY_H}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{p['font']},{p['size']},{WHITE},&H000000FF,"
        f"{BLACK_OUTLINE},&H00000000,1,0,0,0,100,100,0,0,1,{p['outline']},"
        f"{p['shadow']},{p['alignment']},60,60,{p['margin_v']},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    events = []
    if not words:
        with open(ass_path, "w") as f:
            f.write(header)
        return 0

    chunks = [words[i:i + p["chunk"]] for i in range(0, len(words), p["chunk"])]

    for chunk in chunks:
        chunk_end = chunk[-1]["end"]
        for i, w in enumerate(chunk):
            seg_start = w["start"]
            seg_end = chunk[i + 1]["start"] if i + 1 < len(chunk) else chunk_end
            if seg_end <= seg_start:
                seg_end = seg_start + 0.05
            if p["highlight"]:
                parts = []
                for j, ww in enumerate(chunk):
                    if j == i:
                        parts.append(f"{{\\c{p['highlight']}}}{ww['text']}{{\\c{WHITE}}}")
                    else:
                        parts.append(ww["text"])
                line = " ".join(parts)
            else:
                line = " ".join(ww["text"] for ww in chunk)
            events.append(
                f"Dialogue: 0,{fmt_time(seg_start)},{fmt_time(seg_end)},"
                f"Default,,0,0,0,,{line}"
            )

    with open(ass_path, "w") as f:
        f.write(header + "\n".join(events) + "\n")
    return len(events)


def burn_captions(clip_path, ass_path, out_path):
    """Burn ASS captions via libass subtitles filter. Uses bundled fonts."""
    # ASS path needs escaping for ffmpeg filter syntax — colons / commas in
    # paths are filter separators. Forward slashes are fine. Replace any
    # weird chars defensively.
    safe_ass = ass_path.replace("\\", "/").replace(":", r"\:")
    safe_fonts = FONT_DIR.replace("\\", "/").replace(":", r"\:")
    vf = f"subtitles={safe_ass}:fontsdir={safe_fonts}"

    cmd = [
        "ffmpeg", "-y",
        *HW_ACCEL,
        "-i", clip_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "20", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Re-try without hwaccel in case the source codec doesn't roundtrip cleanly
        cmd_nohw = [c for c in cmd if c not in HW_ACCEL]
        r = subprocess.run(cmd_nohw, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"   ✗ ffmpeg burn failed:\n{r.stderr[-400:]}")
        return False
    return True


def collect_clips(args):
    """Return a list of input video paths from CLI args."""
    if not args:
        return sorted(_in_dir(os.path.join(os.getcwd(), "vertical_clips")))
    if len(args) == 1 and os.path.isdir(args[0]):
        return sorted(_in_dir(os.path.join(args[0], "vertical_clips")))
    return [a for a in args if a.endswith(".mp4") and os.path.exists(a)]


def _in_dir(d):
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".mp4")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="*", help="work_dir, directory, or specific .mp4 files")
    ap.add_argument("--style", default="minimal", choices=list(PRESETS.keys()))
    ap.add_argument("--model", default="base.en",
                    help="Whisper model for caption transcription (tiny.en, base.en, small.en)")
    ap.add_argument("--out-dir", default="captioned_clips",
                    help="Output dir name relative to the clip's parent's parent (default: captioned_clips)")
    ap.add_argument("--inplace", action="store_true",
                    help="Overwrite each input clip (skip out-dir routing)")
    args = ap.parse_args()

    clips = collect_clips(args.inputs)
    if not clips:
        print("No input .mp4 clips found.")
        print("Pass a work_dir, a directory containing vertical_clips/, or specific .mp4 paths.")
        sys.exit(1)

    print(f"Style  : {args.style}")
    print(f"Model  : {args.model}")
    print(f"Clips  : {len(clips)}")
    print(f"Fonts  : {FONT_DIR}")

    success = 0
    skipped = 0
    for clip in clips:
        # Determine output path
        if args.inplace:
            out_path = clip
            tmp_out = clip + ".captioned.tmp.mp4"
        else:
            # Output sits alongside the clip's parent dir
            parent = os.path.dirname(os.path.abspath(clip))
            work_dir = os.path.dirname(parent) if os.path.basename(parent) == "vertical_clips" else parent
            out_dir = os.path.join(work_dir, args.out_dir)
            os.makedirs(out_dir, exist_ok=True)
            stem = os.path.splitext(os.path.basename(clip))[0]
            stem = re.sub(r"_vertical$", "", stem)
            out_path = os.path.join(out_dir, f"{stem}_captioned.mp4")
            tmp_out = out_path

        if not args.inplace and os.path.exists(out_path):
            print(f"   ✓ Skip (exists): {os.path.basename(out_path)}")
            skipped += 1
            continue

        print(f"\n▶ {os.path.basename(clip)}")
        with tempfile.TemporaryDirectory(prefix="caps_") as td:
            json_path = transcribe_clip(clip, args.model, td)
            if not json_path:
                continue
            words = load_words(json_path)
            print(f"   {len(words)} word(s) transcribed")
            if not words:
                print("   ⚠ No word timestamps — skipping")
                continue

            ass_path = os.path.join(td, "captions.ass")
            n_events = build_ass(words, ass_path, style=args.style)
            print(f"   {n_events} ASS dialogue event(s)")

            ok = burn_captions(clip, ass_path, tmp_out)
            if not ok:
                continue

        if args.inplace:
            shutil.move(tmp_out, clip)
            print(f"   ✓ captions burned in-place")
        else:
            mb = os.path.getsize(out_path) / 1_000_000
            print(f"   ✓ {os.path.basename(out_path)}  ({mb:.1f} MB)")
        success += 1

    print(f"\n{'='*56}")
    print(f"✓ Captioned : {success}")
    if skipped:
        print(f"  Skipped   : {skipped} (already had captioned output)")


if __name__ == "__main__":
    main()
