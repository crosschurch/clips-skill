#!/usr/bin/env python3
"""
Build an 8-12 minute "sermon recap" — the meat-and-potatoes long-form version
of a sermon with a clear beginning, middle, and end. Inspired by how Steven
Furtick / Elevation post a 10-minute recap alongside the full sermon.

This is NOT aggressive editing. It picks 5-10 LARGE structural segments
(1-3 min each) from the full sermon transcript and concatenates them. Pauses,
natural pacing, and full illustrations stay in. The Claude call thinks about
sermon structure, not punch.

Usage:
    python3 make_sermon_recap.py [work_dir] [--target-minutes 10]

Output:
    <work_dir>/sermon_recap/recap.mp4
    <work_dir>/sermon_recap/manifest.json
"""

import json
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_TARGET_MIN = 10
TARGET_MIN_FLOOR = 8       # minimum total recap length
TARGET_MIN_CEILING = 12    # maximum total recap length
SEGMENT_HEAD_PAD = 0.20    # whisper end timestamps trim consonants — pad both sides
SEGMENT_TAIL_PAD = 0.50
FADE_IN_DUR = 1.5          # picture+audio fade from black/silence at the start
FADE_OUT_DUR = 2.0         # picture+audio fade to black/silence at the end

RECAP_PROMPT = """You are editing a long-form sermon recap for Cross Church. Like Elevation Church's "10-minute highlights" — the meat and potatoes of the sermon in long form, NOT a punchy short. The viewer should feel like they got the full message, just trimmed.

YOUR JOB: Pick 5-10 large structural segments from this sermon transcript that, when concatenated in order, form a complete sermon arc:
- BEGINNING — the opening hook, the question or premise the sermon is built around
- MIDDLE — the central argument, key illustrations, scripture exposition, the meat of the teaching
- END — the resolution, the application, the call/landing

This is NOT a viral clip selection. Pick by structural importance, not by punchiness.

LENGTH RULES (strict):
- Each segment must be 60-180 seconds long (long enough to develop a thought; short enough to keep momentum)
- Total length of all segments combined must be {target_min_floor}-{target_min_ceiling} minutes (i.e. {target_floor_sec}-{target_ceiling_sec} seconds). Aim for ~{target_sec}s.
- Segments must be in chronological order (no reordering)
- No two segments may overlap

CONTENT RULES:
- Cut: long announcements, prayer-for-the-service moments, off-topic asides, repeated points, anything that doesn't serve the sermon's spine
- Keep: the actual teaching, the illustrations the pastor returns to, the scripture readings, the application
- Pauses, "ums", natural pacing, and full illustrations stay in within a kept segment — DO NOT cut inside a segment. We're picking big chunks, not editing tightly.
- Each segment should start at a clean break point (transition phrase, new idea, "so here's the thing", a scripture reference, etc.) and end at the resolution of its thought
- Use ONLY timestamps that appear verbatim in the transcript

TRANSCRIPT (full sermon, with [mm:ss–mm:ss] markers):
{transcript}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "title": "<recap title, under 60 chars>",
  "summary": "<one or two sentences describing the sermon's spine>",
  "segments": [
    {{
      "start": <seconds as number>,
      "end": <seconds as number>,
      "role": "<beginning|middle|end>",
      "label": "<6-12 word description of what this segment covers>"
    }}
  ]
}}"""


def format_transcript(segments):
    lines = []
    for seg in segments:
        s, e = seg["start"], seg["end"]
        m_s = f"{int(s//60)}:{int(s%60):02d}"
        m_e = f"{int(e//60)}:{int(e%60):02d}"
        lines.append(f"[{m_s}–{m_e}] {seg['text'].strip()}")
    return "\n".join(lines)


def ffprobe_duration(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def find_full_sermon(work_dir):
    """Same logic as find_moments.find_full_sermon_video: any non-marker MP4 >10 min."""
    candidates = []
    for f in os.listdir(work_dir):
        if not f.endswith(".mp4"):
            continue
        if "_marker_" in f:
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", f):
            continue  # OBS recording
        path = os.path.join(work_dir, f)
        try:
            dur = ffprobe_duration(path)
        except Exception:
            continue
        if dur > 600:
            candidates.append((dur, path))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]


def ask_claude(prompt, work_dir):
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True,
        cwd=work_dir, timeout=900,
    )
    if result.returncode != 0:
        print(f"✗ claude error: {result.stderr[:300]}")
        return None
    match = re.search(r"\{[\s\S]*\}", result.stdout.strip())
    if not match:
        print(f"✗ No JSON in response. Preview:\n{result.stdout[:400]}")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        print(f"✗ JSON parse error: {e}\nResponse: {result.stdout[:400]}")
        return None


def validate_segments(segments, video_duration, target_floor_sec, target_ceiling_sec):
    """Drop bad segments, sort chronologically, drop overlaps. Return cleaned list + warnings."""
    warnings = []
    cleaned = []
    for i, s in enumerate(segments):
        try:
            start = float(s["start"])
            end = float(s["end"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"segment {i}: missing/bad start or end")
            continue
        dur = end - start
        if start < 0 or end > video_duration + 5:
            warnings.append(f"segment {i}: out of bounds ({start:.0f}–{end:.0f})")
            continue
        if dur < 45 or dur > 200:
            warnings.append(f"segment {i}: duration {dur:.0f}s out of 45-200s range")
            continue
        cleaned.append({
            "start": start,
            "end": end,
            "role": s.get("role", ""),
            "label": s.get("label", ""),
        })

    cleaned.sort(key=lambda x: x["start"])

    # Drop overlaps (keep earlier)
    deduped = []
    for s in cleaned:
        if deduped and s["start"] < deduped[-1]["end"]:
            warnings.append(f"segment at {s['start']:.0f}s overlaps previous — dropping")
            continue
        deduped.append(s)

    total = sum(s["end"] - s["start"] for s in deduped)
    if total < target_floor_sec:
        warnings.append(f"total {total:.0f}s under floor {target_floor_sec}s")
    if total > target_ceiling_sec + 60:
        warnings.append(f"total {total:.0f}s over ceiling {target_ceiling_sec}s (+60s tolerance)")

    return deduped, warnings


def cut_segment(source, start, end, out_path):
    """Re-encode each segment so timestamps reset cleanly and concat can stream-copy."""
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(max(0.0, start - SEGMENT_HEAD_PAD)),
        "-i", source,
        "-t", str(duration + SEGMENT_HEAD_PAD + SEGMENT_TAIL_PAD),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000


def concat_segments(seg_files, out_path, total_duration):
    """Concat segments and apply fade in/out in a single re-encode pass.

    The concat demuxer reads the pre-encoded segment files and feeds the
    combined stream into fade/afade filters. One encode at the output —
    same cost as the previous stream-copy concat would have been if we
    later did a separate fade pass.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        list_path = f.name
        for sp in seg_files:
            f.write(f"file '{sp}'\n")

    fade_out_start = max(0.0, total_duration - FADE_OUT_DUR)
    vf = f"fade=in:st=0:d={FADE_IN_DUR},fade=out:st={fade_out_start}:d={FADE_OUT_DUR}"
    af = f"afade=in:st=0:d={FADE_IN_DUR},afade=out:st={fade_out_start}:d={FADE_OUT_DUR}"

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(list_path)
    if r.returncode != 0:
        print(f"✗ concat failed:\n{r.stderr[-400:]}")
        return False
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    work_dir = args[0] if args else os.getcwd()

    target_min = DEFAULT_TARGET_MIN
    if "--target-minutes" in sys.argv:
        i = sys.argv.index("--target-minutes")
        if i + 1 < len(sys.argv):
            try:
                target_min = float(sys.argv[i + 1])
            except ValueError:
                pass

    target_sec = int(target_min * 60)
    target_floor_sec = TARGET_MIN_FLOOR * 60
    target_ceiling_sec = TARGET_MIN_CEILING * 60

    video = find_full_sermon(work_dir)
    if not video:
        print(f"✗ No full-sermon MP4 (>10 min, non-marker) found in {work_dir}")
        sys.exit(1)

    stem = os.path.splitext(os.path.basename(video))[0]
    transcript_path = os.path.join(work_dir, "transcripts", stem + ".json")
    if not os.path.exists(transcript_path):
        print(f"✗ Transcript not found: {transcript_path}")
        print("  Run transcribe.sh first.")
        sys.exit(1)

    with open(transcript_path) as f:
        data = json.load(f)
    segments = data if isinstance(data, list) else data.get("segments", [])
    if not segments:
        print(f"✗ Transcript has no segments: {transcript_path}")
        sys.exit(1)

    video_duration = ffprobe_duration(video)

    print(f"Sermon   : {os.path.basename(video)}")
    print(f"Duration : {video_duration:.0f}s ({video_duration/60:.1f} min)")
    print(f"Target   : ~{target_sec}s ({target_min:.0f} min), range {target_floor_sec}-{target_ceiling_sec}s")
    print(f"Segments in transcript: {len(segments)}")
    print("\nAsking Claude for structural recap segments...")

    prompt = RECAP_PROMPT.format(
        target_floor_sec=target_floor_sec,
        target_ceiling_sec=target_ceiling_sec,
        target_sec=target_sec,
        target_min_floor=TARGET_MIN_FLOOR,
        target_min_ceiling=TARGET_MIN_CEILING,
        transcript=format_transcript(segments),
    )

    plan = ask_claude(prompt, work_dir)
    if not plan or not plan.get("segments"):
        print("✗ No usable recap plan returned")
        sys.exit(1)

    cleaned, warnings = validate_segments(
        plan["segments"], video_duration, target_floor_sec, target_ceiling_sec,
    )
    for w in warnings:
        print(f"  ⚠ {w}")
    if not cleaned:
        print("✗ No valid segments after validation")
        sys.exit(1)

    out_dir = os.path.join(work_dir, "sermon_recap")
    os.makedirs(out_dir, exist_ok=True)

    total = sum(s["end"] - s["start"] for s in cleaned)
    print(f"\n✓ {len(cleaned)} segments accepted | total {total:.0f}s ({total/60:.1f} min)")
    print(f"  Title  : {plan.get('title', '(none)')}")
    print(f"  Summary: {plan.get('summary', '(none)')}\n")

    for i, s in enumerate(cleaned, start=1):
        sf = f"{int(s['start']//60)}:{int(s['start']%60):02d}"
        ef = f"{int(s['end']//60)}:{int(s['end']%60):02d}"
        role = s.get("role", "").ljust(9)
        print(f"  [{i:02d}] {role} {sf}–{ef} ({s['end']-s['start']:.0f}s) — {s.get('label', '')}")

    print("\nCutting segments...")
    tmpdir = tempfile.mkdtemp()
    seg_files = []
    for i, s in enumerate(cleaned):
        seg_path = os.path.join(tmpdir, f"seg_{i:02d}.mp4")
        if cut_segment(video, s["start"], s["end"], seg_path):
            seg_files.append(seg_path)
            print(f"  ✓ seg {i+1}/{len(cleaned)}")
        else:
            print(f"  ✗ seg {i+1} failed — recap will be missing this chunk")

    if not seg_files:
        print("✗ All segments failed to cut")
        sys.exit(1)

    recap_path = os.path.join(out_dir, "recap.mp4")
    print(f"\nConcatenating to {recap_path}...")
    if not concat_segments(seg_files, recap_path, total):
        sys.exit(1)

    manifest = {
        "source": os.path.basename(video),
        "title": plan.get("title", ""),
        "summary": plan.get("summary", ""),
        "target_minutes": target_min,
        "total_seconds": total,
        "segments": cleaned,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    size_mb = os.path.getsize(recap_path) / 1_000_000
    print(f"\n✓ recap.mp4  ({total:.0f}s, {size_mb:.1f} MB)  →  {recap_path}")
    print(f"✓ manifest    →  {manifest_path}")


if __name__ == "__main__":
    main()
