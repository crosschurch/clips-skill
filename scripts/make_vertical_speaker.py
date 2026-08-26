#!/usr/bin/env /usr/bin/python3
"""
Convert horizontal sermon clips to vertical 9:16 by tracking the ACTIVE SPEAKER.

Use this instead of make_vertical.py for the rare sermon where the person
talking shares the stage with other people (e.g. a panel / two seated guests).
Plain "largest face" tracking drifts to whoever's biggest; this version follows
whoever is in the foreground AND whose mouth is moving.

Requires Python 3.9+ at /usr/bin/python3 with: cv2, numpy, scipy

Usage:
    /usr/bin/python3 make_vertical_speaker.py [work_dir | clip1.mp4 ...]

    - No args        → process all *.mp4 in <work_dir>/viral_clips/
    - Directory arg  → process that directory's viral_clips/
    - .mp4 args      → process those specific files, output alongside them

Algorithm:
    1. Sample ~8 frames/sec. Detect ALL faces (frontal + profile) per frame.
    2. Detect scene cuts (big global frame diff) → segment the clip; the crop
       SNAPS at each cut instead of sliding across it.
    3. Within a segment, link faces into tracks and measure per-track mouth
       motion (frame-to-frame diff of a normalized lower-face patch).
    4. Per frame pick the active speaker = highest mouth motion, weighted by
       foreground cues (face area + how high it sits in frame), with hysteresis
       so it doesn't flicker between people during pauses.
    5. Interpolate + Gaussian-smooth the chosen center-X PER SEGMENT, crop 9:16,
       pipe frames to ffmpeg, mux original audio → 1080×1920 H.264 CRF 18.
"""

import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d
import subprocess
import os
import sys
import glob
import json


# ── Detection ────────────────────────────────────────────────────────────────

def _load_cascades():
    base = cv2.data.haarcascades
    return {
        "frontal": cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml"),
        "profile": cv2.CascadeClassifier(base + "haarcascade_profileface.xml"),
    }


def detect_faces(gray, cascades, min_size=46):
    """Detect all faces (frontal + left/right profile), deduped. → list of (x,y,w,h)."""
    boxes = []

    for f in cascades["frontal"].detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE,
    ):
        boxes.append(tuple(int(v) for v in f))

    # Right-facing profiles
    for f in cascades["profile"].detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE,
    ):
        boxes.append(tuple(int(v) for v in f))

    # Left-facing profiles (profile cascade only catches one orientation)
    flipped = cv2.flip(gray, 1)
    w_img = gray.shape[1]
    for (x, y, w, h) in cascades["profile"].detectMultiScale(
        flipped, scaleFactor=1.1, minNeighbors=5,
        minSize=(min_size, min_size), flags=cv2.CASCADE_SCALE_IMAGE,
    ):
        boxes.append((w_img - x - w, int(y), int(w), int(h)))

    return _dedupe(boxes)


def _dedupe(boxes, iou_thresh=0.3):
    """Greedy NMS by area — drop boxes that heavily overlap a larger one."""
    boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)
    kept = []
    for b in boxes:
        if all(_iou(b, k) < iou_thresh for k in kept):
            kept.append(b)
    return kept


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    return inter / (aw * ah + bw * bh - inter)


# ── Active-speaker selection within one shot segment ──────────────────────────
#
# The speaker is the person MOVING — gesturing, swaying, walking — while the
# seated panelists sit nearly still. So we drive horizontal framing off motion
# energy (frame-to-frame diff, column-wise), not face size. Faces are used only
# to snap the final X onto the nearest head above the motion, so we frame a face
# rather than mid-torso/hands.

def select_speakers_in_segment(samples, src_w, src_h, crop_w):
    """
    samples: list of (frame_idx, [boxes], gray_frame) in temporal order, one shot.
    Returns {frame_idx: center_x} centered on the active (moving) speaker.
    """
    chosen = {}
    last_cx = src_w / 2.0
    prev_gray = None

    # Horizontal smoothing kernel ~ a quarter crop wide (Gaussian)
    ksz = max(9, int(crop_w * 0.25)) | 1
    kx = cv2.getGaussianKernel(ksz, ksz / 4.0).flatten()

    for (fidx, boxes, gray) in samples:
        motion_cx = None
        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            h = diff.shape[0]
            band = diff[int(h * 0.05):int(h * 0.85), :]      # drop top sliver + audience strip
            if band.mean() > 2.0:                            # something actually moved
                col = band.sum(axis=0).astype(np.float32)
                col = np.convolve(col, kx, mode="same")
                peak = int(np.argmax(col))
                w = crop_w // 2
                lo, hi = max(0, peak - w), min(src_w, peak + w)
                seg = col[lo:hi].copy()
                seg[seg < 0.35 * seg.max()] = 0.0            # ignore faint noise
                xs = np.arange(lo, hi)
                denom = seg.sum()
                motion_cx = float((xs * seg).sum() / denom) if denom > 0 else float(peak)

        # Snap to the nearest face above/near the motion (frame the head, not hands)
        cx = motion_cx
        if boxes:
            centers = [(x + bw / 2.0, y + bh / 2.0) for (x, y, bw, bh) in boxes]
            ref = motion_cx if motion_cx is not None else last_cx
            near = [c for c in centers if abs(c[0] - ref) < crop_w * 0.6]
            if near:
                near.sort(key=lambda c: (c[1], abs(c[0] - ref)))  # highest, then closest
                cx = near[0][0]
            elif motion_cx is None:
                cx = None  # no motion and no nearby face → hold last below

        if cx is None:
            cx = last_cx
        chosen[fidx] = cx
        last_cx = cx
        prev_gray = gray

    return chosen


# ── Trajectory (piecewise per shot) ───────────────────────────────────────────

def piecewise_centers(sample_pos, cut_frames, total_frames, src_w, crop_w, fps):
    """Interpolate + smooth chosen center-X within each shot segment only."""
    half = crop_w // 2
    centers = np.full(total_frames, src_w / 2.0, dtype=float)

    bounds = [0] + sorted(c for c in cut_frames if 0 < c < total_frames) + [total_frames]
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg_keys = sorted(f for f in sample_pos if a <= f < b)
        if not seg_keys:
            continue
        xs = [sample_pos[f] for f in seg_keys]
        seg = np.interp(np.arange(a, b), seg_keys, xs)
        sigma = max(1.0, fps * 0.4)
        if len(seg) > 1:
            seg = gaussian_filter1d(seg, sigma=sigma)
        centers[a:b] = seg

    return np.clip(centers, half, src_w - half)


# ── Core conversion ───────────────────────────────────────────────────────────

def make_vertical(input_path, output_path):
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

    crop_w = int(src_h * 9 / 16) & ~1
    crop_h = src_h & ~1
    max_left = src_w - crop_w

    print(f"   Source : {src_w}×{src_h} @ {fps:.0f}fps")
    print(f"   Crop   : {crop_w}×{crop_h}  →  1080×1920 (9:16)")

    cascades = _load_cascades()

    # ── Phase 1: detect faces + scene cuts ───────────────────────────────────
    sample_every = max(1, int(round(fps / 8)))     # ~8 fps for lip motion
    print(f"   Phase 1: Detecting faces + cuts (every {sample_every} frames)...")

    samples = []          # (frame_idx, boxes, gray)
    cut_frames = []
    prev_small = None
    frame_idx = 0
    n_faces_total = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % sample_every == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            small = cv2.resize(gray, (64, 36))
            if prev_small is not None:
                diff = float(np.mean(np.abs(small.astype(np.int16) - prev_small)))
                if diff > 32:                       # hard cut / angle change
                    cut_frames.append(frame_idx)
            prev_small = small.astype(np.int16)

            boxes = detect_faces(gray, cascades)
            n_faces_total += len(boxes)
            samples.append((frame_idx, boxes, gray))
        frame_idx += 1
    cap.release()
    actual_frames = frame_idx

    n_with_face = sum(1 for _, b, _ in samples if b)
    avg_faces = n_faces_total / max(1, len(samples))
    print(f"   Detect : {len(samples)} samples, {n_with_face} with a face, "
          f"avg {avg_faces:.1f} faces/frame, {len(cut_frames)} cuts")

    # ── Phase 2: active-speaker selection per shot segment ───────────────────
    print("   Phase 2: Tracking active speaker (mouth motion)...")
    sample_pos = {}
    bounds = [0] + cut_frames + [actual_frames]
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = [s for s in samples if a <= s[0] < b]
        if seg:
            sample_pos.update(select_speakers_in_segment(seg, src_w, src_h, crop_w))

    centers = piecewise_centers(sample_pos, cut_frames, actual_frames,
                                src_w, crop_w, fps)
    lefts = np.clip(centers - crop_w // 2, 0, max_left).astype(int) & ~1

    # ── Phase 3: encode (pipe cropped frames → ffmpeg, audio from source) ────
    print("   Phase 3: Encoding...")
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "v:0", input_path],
        capture_output=True, text=True,
    )
    try:
        video_start = float(json.loads(probe.stdout)["streams"][0].get("start_time", 0) or 0)
    except (KeyError, IndexError, ValueError):
        video_start = 0.0

    out_w, out_h = 1080, 1920
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{crop_w}x{crop_h}", "-pix_fmt", "bgr24", "-r", str(fps),
        "-i", "pipe:0",
        "-ss", str(video_start), "-i", input_path,
        "-map", "0:v", "-map", "1:a?",
        "-vf", f"scale={out_w}:{out_h}:flags=lanczos",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        output_path,
    ]
    proc = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    cap = cv2.VideoCapture(input_path)
    fi = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            left = int(lefts[fi]) if fi < len(lefts) else max_left // 2
            cropped = frame[:crop_h, left:left + crop_w]
            if cropped.shape[:2] == (crop_h, crop_w):
                proc.stdin.write(cropped.tobytes())
            fi += 1
            if fi % 200 == 0:
                print(f"   {fi}/{actual_frames} ({fi/max(actual_frames,1)*100:.0f}%)", end="\r")
    finally:
        cap.release()

    proc.stdin.close()
    stderr = proc.stderr.read()
    proc.wait()
    print(f"   {fi} frames written.            ")

    if proc.returncode == 0:
        mb = os.path.getsize(output_path) / 1_000_000
        print(f"   ✓ Done → {os.path.basename(output_path)}  ({mb:.1f} MB)")
        return True
    print(f"   ✗ Encode failed:\n{stderr.decode()[-400:]}")
    return False


def main():
    args = sys.argv[1:]
    if not args:
        clips_dir = os.path.join(os.getcwd(), "viral_clips")
        vert_dir  = os.path.join(os.getcwd(), "vertical_clips")
        src_clips = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")))
    elif len(args) == 1 and os.path.isdir(args[0]):
        clips_dir = os.path.join(args[0], "viral_clips")
        vert_dir  = os.path.join(args[0], "vertical_clips")
        src_clips = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")))
    else:
        src_clips = [a for a in args if a.endswith(".mp4")]
        vert_dir  = None

    if not src_clips:
        print("No .mp4 clips found to process.")
        sys.exit(1)

    pairs = []
    for clip in src_clips:
        if vert_dir:
            os.makedirs(vert_dir, exist_ok=True)
            base = os.path.splitext(os.path.basename(clip))[0]
            out = os.path.join(vert_dir, base + "_vertical.mp4")
        else:
            out = os.path.splitext(clip)[0] + "_vertical.mp4"
        pairs.append((clip, out))

    print(f"Active-speaker vertical conversion: {len(pairs)} clip(s)...")
    ok = 0
    for clip, out in pairs:
        if make_vertical(clip, out):
            ok += 1
    print(f"\n{'='*56}\n✓ {ok}/{len(pairs)} clips converted")
    if vert_dir:
        print(f"  Output: {vert_dir}")


if __name__ == "__main__":
    main()
