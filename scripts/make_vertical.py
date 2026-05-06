#!/usr/bin/env /usr/bin/python3
"""
Convert horizontal sermon clips to vertical 9:16 with AI subject tracking.

Requires Python 3.9 at /usr/bin/python3 with: cv2, numpy, scipy

Usage:
    /usr/bin/python3 make_vertical.py [work_dir | clip1.mp4 clip2.mp4 ...]

    - No args           → process all *.mp4 in <work_dir>/viral_clips/
    - Directory arg     → process that directory's viral_clips/
    - .mp4 args         → process those specific files, output alongside them

Algorithm:
    1. Sample ~4 frames/sec → detect face (primary) or upper body (fallback)
    2. Interpolate positions across all frames
    3. Gaussian smooth (sigma=1.5s) → mimics a slow camera pan
    4. Write cropped frames to temp file via OpenCV
    5. Merge with original audio → final H.264 CRF 18
"""

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d
import subprocess
import os
import sys
import glob
import json
import tempfile


def detect_subject_center(frame, face_cas, upper_cas):
    """Returns (center_x, method) or (None, 'none')."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    faces = face_cas.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=4,
        minSize=(30, 30), flags=cv2.CASCADE_SCALE_IMAGE,
    )
    if len(faces) > 0:
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return x + w // 2, "face"

    upper = upper_cas.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=3,
        minSize=(60, 60), flags=cv2.CASCADE_SCALE_IMAGE,
    )
    if len(upper) > 0:
        x, y, w, h = max(upper, key=lambda u: u[2] * u[3])
        return x + w // 2, "upper"

    return None, "none"


def smooth_trajectory(sample_positions, total_frames, src_w, crop_w, fps):
    """Interpolate and smooth center-X positions across all frames."""
    if not sample_positions:
        return np.full(total_frames, src_w // 2, dtype=float)

    frames_known = sorted(sample_positions)
    xs_known = [sample_positions[f] for f in frames_known]

    centers = np.interp(np.arange(total_frames), frames_known, xs_known)
    half = crop_w // 2
    centers = np.clip(centers, half, src_w - half)

    # 0.5-second Gaussian window — tighter follow, less lag
    centers = gaussian_filter1d(centers, sigma=fps * 0.5)
    return centers


def make_vertical(input_path, output_path):
    """Core conversion: horizontal clip → 9:16 vertical with subject tracking."""
    label = os.path.basename(input_path)
    print(f"\n{'─'*56}")
    print(f"▶  {label}")

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        print("   ✗ Cannot open video")
        return False

    fps      = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # 9:16 crop dimensions — keep full height, crop width
    crop_w = int(src_h * 9 / 16) & ~1   # ensure even
    crop_h = src_h & ~1
    max_left = src_w - crop_w

    print(f"   Source : {src_w}×{src_h} @ {fps:.0f}fps  ({n_frames} frames)")
    print(f"   Crop   : {crop_w}×{crop_h}  →  scale → 1080×1920 (9:16)")

    # Load Haar classifiers (built into OpenCV, no download needed)
    face_cas = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    upper_cas = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_upperbody.xml"
    )

    # ── Phase 1: Detection pass ──────────────────────────────────────────────
    sample_every = max(1, int(fps / 4))
    sample_pos   = {}
    stats        = {"face": 0, "upper": 0, "none": 0}

    print(f"   Phase 1: Detecting subject (every {sample_every} frames)...")
    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_every == 0:
            cx, method = detect_subject_center(frame, face_cas, upper_cas)
            if cx is not None:
                sample_pos[frame_idx] = cx
            stats[method] = stats.get(method, 0) + 1
        frame_idx += 1

    cap.release()
    actual_frames = frame_idx

    hit_pct = (stats["face"] + stats["upper"]) / max(1, sum(stats.values()))
    print(f"   Detect : face={stats['face']} body={stats['upper']} miss={stats['none']} "
          f"({hit_pct:.0%} hit)")
    if hit_pct < 0.15:
        print("   ⚠  Low detection — falling back to center crop")

    # ── Phase 2: Build smooth trajectory ─────────────────────────────────────
    centers   = smooth_trajectory(sample_pos, actual_frames, src_w, crop_w, fps)
    lefts     = np.clip(centers - crop_w // 2, 0, max_left).astype(int)
    lefts    &= ~1  # keep even

    # ── Phase 3: Pipe cropped frames into ffmpeg (audio stays in sync) ──────────
    # -c copy clips have a video start_time offset (keyframe snap) while audio
    # starts at 0. We detect that offset and seek the audio input to match,
    # so both streams align with the first frame OpenCV reads.
    print("   Phase 3: Encoding (piping frames → ffmpeg)...")

    # Get video stream start_time so we can seek audio to the same point
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "v:0", input_path],
        capture_output=True, text=True,
    )
    try:
        video_start = float(json.loads(probe.stdout)["streams"][0].get("start_time", 0) or 0)
    except (KeyError, IndexError, ValueError):
        video_start = 0.0

    if video_start > 0:
        print(f"   Sync fix: video offset {video_start:.3f}s — seeking audio to match")

    # Target canvas for social-native 9:16. Upscale the crop with lanczos
    # so the encoded file is 1080×1920 regardless of source height.
    out_w, out_h = 1080, 1920
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        # Video: raw BGR frames from stdin at exact source fps
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{crop_w}x{crop_h}",
        "-pix_fmt", "bgr24",
        "-r", str(fps),
        "-i", "pipe:0",
        # Audio: seek to video_start so it aligns with the first piped frame
        "-ss", str(video_start),
        "-i", input_path,
        "-map", "0:v",        # video from pipe
        "-map", "1:a?",       # audio from source (? = ok if missing)
        "-vf", f"scale={out_w}:{out_h}:flags=lanczos",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path,
    ]

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    cap = cv2.VideoCapture(input_path)
    fi = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            left    = int(lefts[fi]) if fi < len(lefts) else max_left // 2
            cropped = frame[:crop_h, left : left + crop_w]
            if cropped.shape[:2] == (crop_h, crop_w):
                ffmpeg_proc.stdin.write(cropped.tobytes())
            fi += 1
            if fi % 200 == 0:
                pct = fi / max(actual_frames, 1) * 100
                print(f"   {fi}/{actual_frames} ({pct:.0f}%)", end="\r")
    finally:
        cap.release()

    # Close stdin once, then read stderr and wait — don't call communicate() after manual close
    ffmpeg_proc.stdin.close()
    stderr = ffmpeg_proc.stderr.read()
    ffmpeg_proc.wait()
    print(f"   {fi} frames written.            ")

    if ffmpeg_proc.returncode == 0:
        mb = os.path.getsize(output_path) / 1_000_000
        print(f"   ✓ Done → {os.path.basename(output_path)}  ({mb:.1f} MB)")
        return True
    else:
        print(f"   ✗ Encode failed:\n{stderr.decode()[-400:]}")
        return False


def main():
    # Resolve input clips and output paths
    clips_to_process = []   # list of (input_path, output_path)

    args = sys.argv[1:]

    if not args:
        # No args → find viral_clips/ relative to CWD
        clips_dir   = os.path.join(os.getcwd(), "viral_clips")
        vert_dir    = os.path.join(os.getcwd(), "vertical_clips")
        src_clips   = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")))
    elif len(args) == 1 and os.path.isdir(args[0]):
        # Directory arg → find viral_clips/ inside it
        work_dir    = args[0]
        clips_dir   = os.path.join(work_dir, "viral_clips")
        vert_dir    = os.path.join(work_dir, "vertical_clips")
        src_clips   = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")))
    else:
        # Explicit file paths
        src_clips   = [a for a in args if a.endswith(".mp4")]
        vert_dir    = None

    if not src_clips:
        print("No .mp4 clips found to process.")
        sys.exit(1)

    # Build (input, output) pairs
    for clip in src_clips:
        if vert_dir:
            os.makedirs(vert_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(clip))[0]
            out  = os.path.join(vert_dir, base + "_vertical.mp4")
            # The rank-rename step prepends "NN_" to vertical filenames, so
            # an existing rank-prefixed file must count as "already done".
            ranked = glob.glob(os.path.join(vert_dir, f"??_{base}_vertical.mp4"))
        else:
            base = os.path.splitext(clip)[0]
            out  = base + "_vertical.mp4"
            ranked = []

        if os.path.exists(out) or ranked:
            shown = os.path.basename(ranked[0]) if ranked else os.path.basename(out)
            print(f"✓ Already done (skip): {shown}")
        else:
            clips_to_process.append((clip, out))

    if not clips_to_process:
        print("All clips already have a vertical version.")
        sys.exit(0)

    print(f"Converting {len(clips_to_process)} clip(s) to vertical 9:16...")

    successes = 0
    for clip_path, out_path in clips_to_process:
        if make_vertical(clip_path, out_path):
            successes += 1

    print(f"\n{'='*56}")
    print(f"✓ {successes}/{len(clips_to_process)} clips converted")
    if vert_dir:
        print(f"  Output: {vert_dir}")

    # Update manifest if present
    manifest_path = None
    for clip, _ in clips_to_process:
        candidate = os.path.join(os.path.dirname(clip), "moments.json")
        if os.path.exists(candidate):
            manifest_path = candidate
            break

    if manifest_path:
        with open(manifest_path) as f:
            moments = json.load(f)

        # Update vertical_file paths before ranking
        for m in moments:
            base = os.path.splitext(m.get("file", ""))[0]
            if vert_dir:
                vert = os.path.join(vert_dir, base + "_vertical.mp4")
            else:
                clip_dir = os.path.dirname(moments[0].get("file", ""))
                vert = os.path.join(clip_dir, base + "_vertical.mp4")
            if os.path.exists(vert):
                m["vertical_file"] = os.path.basename(vert)

        # Rank by virality score and rename with numeric prefix
        if vert_dir:
            seen_titles = {}
            for m in moments:
                title = m.get("title", "")
                score = m.get("virality_total", 0)
                if title not in seen_titles or score > seen_titles[title]["virality_total"]:
                    seen_titles[title] = m

            ranked = sorted(seen_titles.values(), key=lambda x: x.get("virality_total", 0), reverse=True)
            rank_assigned = 0
            print(f"\n  Ranking {len(ranked)} clips by virality score:")
            for rank, m in enumerate(ranked, start=1):
                base = os.path.splitext(m.get("file", ""))[0]
                old_vert = os.path.join(vert_dir, base + "_vertical.mp4")
                if not os.path.exists(old_vert):
                    continue
                rank_assigned += 1
                score = m.get("virality_total", 0)
                new_name = f"{rank_assigned:02d}_{base}_vertical.mp4"
                new_vert = os.path.join(vert_dir, new_name)
                os.rename(old_vert, new_vert)
                m["vertical_file"] = new_name
                print(f"  {rank_assigned:02d}. [{score}/100] {m.get('title', base)}")

            # Write all clips sorted by virality (not just those with vertical files)
            with open(manifest_path, "w") as f:
                json.dump(ranked, f, indent=2)
        else:
            with open(manifest_path, "w") as f:
                json.dump(moments, f, indent=2)

        print(f"  Manifest updated: {manifest_path}")


if __name__ == "__main__":
    main()
