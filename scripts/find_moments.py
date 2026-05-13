#!/usr/bin/env python3
"""
Reads Whisper transcripts from a working directory, asks Claude to identify
the best viral sermon moments, then cuts each with ffmpeg.

Usage:
    python3 find_moments.py [work_dir] [--edited]
    work_dir defaults to CWD

    --edited   Generate multi-segment edited clips per marker (fluff cut out)
               instead of single-window simple cuts. Edited verticals become
               the Descript upload set. Costs more Claude calls; opt in only
               when shorts-quality output is the goal.

Output:
    <work_dir>/viral_clips/*.mp4
    <work_dir>/viral_clips/moments.json
"""

import json
import os
import subprocess
import glob
import re
import sys

EDITED_MODE = "--edited" in sys.argv
_positional = [a for a in sys.argv[1:] if not a.startswith("--")]
WORK_DIR = _positional[0] if _positional else os.getcwd()
TRANSCRIPTS_DIR = os.path.join(WORK_DIR, "transcripts")
CLIPS_DIR = os.path.join(WORK_DIR, "viral_clips")
os.makedirs(CLIPS_DIR, exist_ok=True)

# Hardware-accelerated decode on macOS — drops decode time substantially on
# the re-encode paths (highlight reel teaser cuts, edited multi-segment cuts).
# No-op on Linux/Windows; ffmpeg silently ignores when the slot is empty.
HW_ACCEL = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []

# ── Cross Church editorial preferences ──────────────────────────────────────
EDITORIAL_PROMPT = """You are a social media editor for Cross Church. Find the best viral moments from a pastor's sermon for Instagram Reels, TikTok, and YouTube Shorts.

GROUNDING RULES (follow strictly):
- Use ONLY timestamps that appear verbatim in the transcript
- Never merge non-contiguous lines into one clip
- Never invent content, tone, or context not in the transcript
- start must be less than end; minimum clip duration 40 seconds
- No two clips may share more than 10 seconds of overlapping content — pick the best window and move on

THOUGHT-COMPLETENESS RULES (the most important rules — read carefully):
A clip is not a sentence range, it is a complete self-contained THOUGHT. A thought is the full idea the speaker is developing — setup, point, payoff. The clip must be the entire thought and *only* that thought.

- start: the speaker is BEGINNING a new idea. The line before the start must be a clean break — a transition phrase, the end of a different thought, or a natural pivot. Never start mid-explanation.
- end: the speaker has just FINISHED making the point. The line *after* your chosen end must be the start of a different idea (a new topic, a transition, "and so", "but here's the thing", a new analogy, a new scripture). If the next line is still developing the same thought (e.g. continues a list, gives the next beat of a story, finishes a sentence the speaker started), your end is too early — extend it or pick a different boundary.
- A ~6 second buffer of footage will be added on each side as editing headroom. The editor will trim it down in post. Your picked timestamps must be the true thought boundaries, not adjusted for the buffer — the buffer is intentional spillover, not a fudge factor.
- Verify before finalizing: re-read the line at end+1 to end+3 in the transcript. Ask "is this the start of a different idea, or the speaker continuing what came before?" If it's a continuation, your end is wrong — extend until the thought truly resolves.

CLIP INFO:
Name: {clip_name}
Duration: {duration:.0f} seconds

TIMESTAMPED TRANSCRIPT:
{transcript}

Find the best 3-6 moments. Each clip must:
- Be 45-90 seconds long (strictly — reject under 40s or over 95s). Err longer when needed to fully resolve the thought; the editor will trim down in post.
- Be ONE complete self-contained thought — see the THOUGHT-COMPLETENESS RULES above. The clip must contain the entire idea (setup → point → payoff) and nothing from the next idea
- The line right after your end timestamp must be the start of a DIFFERENT idea, not a continuation of the current one
- Open with a strong hook: question, bold claim, or story start
- Carry one clear emotional arc (conviction→hope, pain→relief, confusion→clarity)
- A first-time viewer understands it cold and feels the thought has *resolved*
- Prefer stories, analogies, and relatable human moments over doctrine
- Avoid overlapping content with other clips in your response — each clip covers distinct territory

VIRALITY SCORING — score every moment on four dimensions (0-25 each):
1. hook_score: How attention-grabbing is the opening? (surprising, bold, or curious)
2. engagement_score: How emotionally gripping or entertaining is the content?
3. value_score: Does this teach, challenge, or transform the viewer?
4. shareability_score: Would someone text this to a friend? ("you need to hear this")

HOOK TYPE — classify each clip's opening as one of:
  question | statement | statistic | story | contrast | none

HIGHLIGHT BANGER — for each moment, identify the single most punchy, standalone 1-3 sentence statement in the clip. It must be a COMPLETE thought that lands hard with zero surrounding context — not a setup, not a teaser, not a question that goes unanswered. Think: the one sentence someone would screenshot and text to a friend.

QUOTE DUMP — separately, identify 2-5 standalone QUOTES from this transcript suitable for social media quote-card images (plain text, no video needed). A quote must:
- Be 1-3 sentences, 8-220 characters total
- Be a complete self-contained thought that lands with zero context
- Read powerfully as pure text — strip filler ("you know", "I mean", "uh", false starts, mid-sentence corrections). Lightly clean punctuation and capitalization so it reads as written prose, but don't paraphrase or invent words the speaker didn't say
- Avoid quotes that depend on a story setup, a previous illustration, or a "this" / "that" / "he said" without antecedent
- Prefer convicting one-liners, reframes, paradoxes, or sticky truth statements over narrative beats
- Quotes can come from the same content as a moment — they are independent of clip selection

QUOTE STYLE — for each quote, pick the visual style whose energy matches the quote. All styles are clean and minimal (sentence case, generous whitespace, modest type) — they differ in palette and emphasis mechanic, not loudness:
- accent_payoff   → declarative / call-to-action ("Stop X. Reach for Y."). Sans setup → italic warm-gold payoff on dark
- soft_paper      → reframe / paradox / "X is God's Y" wisdom. Cream paper, single emphasis word italicized in-line
- editorial_split → setup→payoff sermonic build ("X if Y", "X but Y", multi-sentence). Charcoal bg, dim setup → bright payoff
- brand_block     → calm identity statement, one or two short sentences. Deep navy bg, sentence case, centered
- scripture_card  → scripture, prayer-language, contemplative, sacred-feeling. Subtle gradient, italic serif
- minimal_serif   → literary, poetic, anything that doesn't fit the others. Dark bg, serif, the safe default

QUOTE PUNCH — for accent_payoff / soft_paper / editorial_split, also pick a "punch":
- accent_payoff   → the final clause / payoff after the setup (becomes the italic gold line)
- soft_paper      → a SINGLE word (the most charged noun in the quote). After removing this word in your head, the rest must still read as a grammatical phrase
- editorial_split → the final clause / payoff after the setup (becomes the bright line under the dim setup)
- For brand_block, scripture_card, minimal_serif: omit "punch" (set to null or "")
The punch MUST be a verbatim substring of the quote text — do not rephrase

Return ONLY valid JSON (no markdown, no explanation). Sort moments by total virality score descending:
{{
  "moments": [
    {{
      "start": <seconds as number>,
      "end": <seconds as number>,
      "title": "<punchy title under 55 chars>",
      "hook": "<exact first words of the clip>",
      "hook_type": "<question|statement|statistic|story|contrast|none>",
      "hook_score": <0-25>,
      "engagement_score": <0-25>,
      "value_score": <0-25>,
      "shareability_score": <0-25>,
      "virality_total": <sum of four scores 0-100>,
      "why": "<one sentence on what makes this shareable>",
      "teaser_start": <seconds — start of the standalone banger statement>,
      "teaser_end": <seconds — end of that complete thought, typically 5-12s after teaser_start>
    }}
  ],
  "quotes": [
    {{
      "text": "<punchy standalone quote, 1-3 sentences, lightly cleaned for readability>",
      "style": "<accent_payoff|soft_paper|editorial_split|brand_block|scripture_card|minimal_serif>",
      "punch": "<verbatim substring of text — see QUOTE PUNCH rules above; omit or empty for brand_block/scripture_card/minimal_serif>"
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


def ask_claude(transcript_text, clip_name, clip_duration):
    prompt = EDITORIAL_PROMPT.format(
        clip_name=clip_name,
        duration=clip_duration,
        transcript=transcript_text,
    )

    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        cwd=WORK_DIR,
        timeout=600,
    )

    if result.returncode != 0:
        print(f"  ✗ claude error: {result.stderr[:200]}")
        return [], []

    response = result.stdout.strip()
    match = re.search(r"\{[\s\S]*\}", response)
    if not match:
        print(f"  ✗ No JSON in response. Preview:\n{response[:400]}")
        return [], []

    try:
        data = json.loads(match.group(0))
        return data.get("moments", []), data.get("quotes", [])
    except json.JSONDecodeError as e:
        print(f"  ✗ JSON parse error: {e}\nResponse: {response[:400]}")
        return [], []


def ask_claude_edited(transcript_text, clip_name, clip_duration):
    """Separate focused Claude call for multi-segment edited clips."""
    prompt = f"""You are a video editor for Cross Church. Identify 3-5 tightly-edited sermon clips (45-70 seconds total each) from this transcript.

Unlike simple clips, these pull non-contiguous segments from the same source to tell one complete story — cutting filler, repeated phrases, and tangents. Segments may be re-ordered for better narrative flow.

CLIP: {clip_name}
DURATION: {clip_duration:.0f} seconds

TIMESTAMPED TRANSCRIPT:
{transcript_text}

Rules:
- Total of all segments must be 45-70 seconds
- Each segment ≥ 5 seconds, no overlaps
- Cut: repeated phrases, filler transitions, tangents, awkward restarts
- Each clip: clear arc — setup → tension → resolution
- Use ONLY timestamps verbatim from the transcript

Return ONLY valid JSON:
{{
  "edited_clips": [
    {{
      "title": "<punchy title under 55 chars>",
      "why": "<one sentence on what makes this worth tighter editing>",
      "segments": [
        {{"start": <seconds>, "end": <seconds>}},
        {{"start": <seconds>, "end": <seconds>}}
      ]
    }}
  ]
}}"""

    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True,
        cwd=WORK_DIR, timeout=300,
    )
    if result.returncode != 0:
        print(f"  ✗ edited clips error: {result.stderr[:200]}")
        return []
    match = re.search(r"\{[\s\S]*\}", result.stdout.strip())
    if not match:
        return []
    try:
        return json.loads(match.group(0)).get("edited_clips", [])
    except json.JSONDecodeError:
        return []


CLIP_BUFFER = 6.0  # generous editing headroom — Josh trims clips down in Descript, so the cut should give him material on both sides to work with. Combined with the THOUGHT-COMPLETENESS rules in the prompt, this means: clip ends at a real thought boundary, then 6s of "next thought start" gives the editor wiggle room to land the cut tightly.

# Whisper segment-level `end` timestamps tend to land on or just before the final
# consonant — words sound chopped if we cut exactly there. Pad each edited-clip
# segment's tail so the word lands. Head pad covers the inverse case where the
# first phoneme starts a beat before the transcript marker.
EDITED_SEG_HEAD_PAD = 0.15
EDITED_SEG_TAIL_PAD = 0.40


def make_highlight_reel(all_moments, work_dir, clips_dir, n_clips=4):
    """Concatenate teaser hooks from the top n clips into a horizontal highlight reel."""
    # Pick top moments that have valid teaser timestamps, from different source clips
    # (already deduplicated by overlap detection, but ensure source variety)
    eligible = [
        m for m in sorted(all_moments, key=lambda x: x.get("virality_total", 0), reverse=True)
        if m.get("teaser_start") is not None and m.get("teaser_end") is not None
        and 4 <= float(m["teaser_end"]) - float(m["teaser_start"]) <= 15
    ]

    if not eligible:
        print("\n⚠ No valid teaser timestamps — skipping highlight reel")
        return

    # Spread across source clips: prefer one teaser per source, then fill from top
    seen_sources = set()
    picks = []
    for m in eligible:
        if m["source"] not in seen_sources:
            picks.append(m)
            seen_sources.add(m["source"])
        if len(picks) >= n_clips:
            break
    # If we didn't get enough from spread, fill from remaining
    if len(picks) < n_clips:
        for m in eligible:
            if m not in picks:
                picks.append(m)
            if len(picks) >= n_clips:
                break

    print(f"\n{'='*58}")
    print(f"Building highlight reel from {len(picks)} teaser hooks...")

    import tempfile
    tmpdir = tempfile.mkdtemp()
    segment_files = []

    for i, m in enumerate(picks):
        source = os.path.join(work_dir, m["source"])
        ts = float(m["teaser_start"])
        te = float(m["teaser_end"])
        dur = te - ts
        seg_path = os.path.join(tmpdir, f"seg_{i:02d}.mp4")

        cmd = [
            "ffmpeg", "-y",
            *HW_ACCEL,
            "-ss", str(ts), "-i", source,
            "-t", str(dur),
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            seg_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and os.path.getsize(seg_path) > 1000:
            segment_files.append(seg_path)
            s_fmt = f"{int(ts//60)}:{int(ts%60):02d}"
            e_fmt = f"{int(te//60)}:{int(te%60):02d}"
            print(f"  [{i+1}] {s_fmt}–{e_fmt} ({dur:.0f}s) — {m['title']}")
        else:
            print(f"  ✗ Segment cut failed for: {m['title']}")

    if not segment_files:
        print("  ✗ No segments — highlight reel skipped")
        return

    # Write concat list
    list_path = os.path.join(tmpdir, "concat.txt")
    with open(list_path, "w") as f:
        for seg in segment_files:
            f.write(f"file '{seg}'\n")

    out_path = os.path.join(clips_dir, "highlight_reel.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        total_dur = sum(float(m["teaser_end"]) - float(m["teaser_start"]) for m in picks if m in picks)
        size_mb = os.path.getsize(out_path) / 1_000_000
        print(f"  ✓ highlight_reel.mp4  ({total_dur:.0f}s, {size_mb:.1f} MB)")
    else:
        print(f"  ✗ Concat failed:\n{result.stderr[-300:]}")


def cut_clip(source_video, start, end, output_path, video_duration=None):
    """Stream-copy cut with buffer padding on each side for editing headroom."""
    padded_start = max(0.0, start - CLIP_BUFFER)
    padded_end = end + CLIP_BUFFER
    if video_duration:
        padded_end = min(padded_end, video_duration)
    duration = padded_end - padded_start

    seek = max(0.0, padded_start - 0.5)
    inner_start = padded_start - seek

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek),
        "-i", source_video,
        "-ss", str(inner_start),
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def get_duration(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


def safe_filename(title):
    s = re.sub(r"[^\w\s\-]", "", title)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:60]


FULL_SERMON_CHUNK_SIZE = 240    # 4-minute chunks for full sermon (matches marker clip budget)
FULL_SERMON_CHUNK_OVERLAP = 30  # 30s overlap between chunks


def find_full_sermon_video(work_dir):
    """Find the full sermon: any MP4 >10min that is not a marker clip or OBS recording."""
    candidates = []
    for f in os.listdir(work_dir):
        if not f.endswith('.mp4'):
            continue
        if '_marker_' in f:
            continue
        if re.match(r'^\d{4}-\d{2}-\d{2}', f):
            continue  # OBS recording
        path = os.path.join(work_dir, f)
        try:
            dur = get_duration(path)
        except Exception:
            continue
        if dur > 600:  # >10 minutes
            candidates.append((dur, path))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][1]  # longest qualifying file


def process_full_sermon(video_path, transcript_path, clips_dir):
    """
    Chunk the full sermon transcript into 6-minute windows, ask Claude for viral
    moments per chunk, convert chunk-relative timestamps to absolute, cut clips.
    Returns (moment dicts, quote strings) — quotes are aggregated across chunks.
    """
    print(f"\n{'='*58}")
    print(f"Full sermon: {os.path.basename(video_path)}")

    with open(transcript_path) as f:
        data = json.load(f)
    all_segs = data if isinstance(data, list) else data.get('segments', [])

    total_duration = get_duration(video_path)
    print(f"  {total_duration:.0f}s ({total_duration/60:.1f} min) | {len(all_segs)} transcript segments")

    sermon_moments = []
    sermon_quotes = []
    accepted_ranges = []  # within-sermon overlap detection (separate from marker clip pool)
    total_cut = 0
    chunk_num = 0
    chunk_start = 0.0

    while chunk_start < total_duration:
        chunk_end = min(chunk_start + FULL_SERMON_CHUNK_SIZE, total_duration)
        chunk_dur = chunk_end - chunk_start

        # Pull segments that start within this chunk
        chunk_segs = [s for s in all_segs if chunk_start <= s['start'] < chunk_end]
        if len(chunk_segs) < 5:
            chunk_start += FULL_SERMON_CHUNK_SIZE - FULL_SERMON_CHUNK_OVERLAP
            continue

        # Normalise to chunk-relative timestamps for Claude
        rel_segs = [{
            'start': s['start'] - chunk_start,
            'end': min(s['end'] - chunk_start, chunk_dur),
            'text': s['text'],
        } for s in chunk_segs]

        chunk_num += 1
        chunk_label = (f"full_sermon_{int(chunk_start//60):02d}m"
                       + (" [may include intro/teaser before main sermon]" if chunk_num == 1 else ""))
        transcript_text = format_transcript(rel_segs)

        s_fmt = f"{int(chunk_start//60)}:{int(chunk_start%60):02d}"
        e_fmt = f"{int(chunk_end//60)}:{int(chunk_end%60):02d}"
        print(f"\n  Chunk {chunk_num}: {s_fmt}–{e_fmt} | {len(chunk_segs)} segs | asking Claude...")

        moments, quotes = ask_claude(transcript_text, chunk_label, chunk_dur)
        sermon_quotes.extend(quotes)
        if not moments:
            print("    No moments found")
            chunk_start += FULL_SERMON_CHUNK_SIZE - FULL_SERMON_CHUNK_OVERLAP
            continue

        for i, m in enumerate(moments):
            rel_start = float(m.get('start', 0))
            rel_end = float(m.get('end', 0))
            title = m.get('title', f'Moment {i+1}')
            dur = rel_end - rel_start

            if dur < 35 or dur > 80:
                print(f"    ⚠ Skip '{title}' — {dur:.0f}s out of range")
                continue
            if rel_start < 0 or rel_end > chunk_dur + 10:
                print(f"    ⚠ Skip '{title}' — out of bounds")
                continue

            abs_start = chunk_start + rel_start
            abs_end = chunk_start + rel_end

            if overlaps(abs_start, abs_end, accepted_ranges):
                print(f"    ⚠ Skip '{title}' — overlaps")
                continue

            fname = safe_filename(title) + ".mp4"
            out = os.path.join(clips_dir, fname)
            if os.path.exists(out):
                out = os.path.join(clips_dir, safe_filename(title) + f"_{chunk_num}.mp4")

            s_abs = f"{int(abs_start//60)}:{int(abs_start%60):02d}"
            e_abs = f"{int(abs_end//60)}:{int(abs_end%60):02d}"
            print(f"    [{i+1}] {s_abs}–{e_abs} ({dur:.0f}s) — {title}")

            if cut_clip(video_path, abs_start, abs_end, out, video_duration=total_duration):
                vt = m.get('virality_total',
                    m.get('hook_score', 0) + m.get('engagement_score', 0) +
                    m.get('value_score', 0) + m.get('shareability_score', 0))
                print(f"         ✓ {os.path.basename(out)}  [virality: {vt}/100]")
                total_cut += 1
                accepted_ranges.append((abs_start, abs_end))

                # Convert teaser timestamps to absolute coords for the highlight reel
                ts = m.get('teaser_start')
                te = m.get('teaser_end')
                sermon_moments.append({
                    'source': os.path.basename(video_path),
                    'start': abs_start,
                    'end': abs_end,
                    'duration': dur,
                    'title': title,
                    'hook': m.get('hook', ''),
                    'hook_type': m.get('hook_type', 'none'),
                    'hook_score': m.get('hook_score', 0),
                    'engagement_score': m.get('engagement_score', 0),
                    'value_score': m.get('value_score', 0),
                    'shareability_score': m.get('shareability_score', 0),
                    'virality_total': vt,
                    'why': m.get('why', ''),
                    'teaser_start': chunk_start + float(ts) if ts is not None else None,
                    'teaser_end': chunk_start + float(te) if te is not None else None,
                    'file': os.path.basename(out),
                    'vertical_file': None,
                    'source_type': 'full_sermon',
                })
            else:
                print("         ✗ Cut failed")

        chunk_start += FULL_SERMON_CHUNK_SIZE - FULL_SERMON_CHUNK_OVERLAP

    print(f"\n  ✓ {total_cut} clips from full sermon")
    print(f"  ✓ {len(sermon_quotes)} raw quotes from full sermon")
    return sermon_moments, sermon_quotes


def marker_abs_start(stem):
    """Parse absolute recording position from marker filename (last HH-MM-SS group)."""
    m = re.search(r'(\d{2})-(\d{2})-(\d{2})$', stem)
    if not m:
        return 0.0
    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mn * 60 + s


VALID_QUOTE_STYLES = {
    "accent_payoff", "soft_paper", "editorial_split",
    "brand_block", "scripture_card", "minimal_serif",
}
# Old style names still accepted (renderer remaps them too).
LEGACY_QUOTE_STYLE_MAP = {
    "grunge_accent":  "accent_payoff",
    "vintage_press":  "soft_paper",
    "editorial_wide": "editorial_split",
}


def _quote_to_dict(q):
    """Coerce a quote entry (str or dict) to {text, style?, punch?}."""
    if isinstance(q, str):
        t = q.strip().strip('"').strip("'").strip()
        return {"text": t} if t else None
    if isinstance(q, dict):
        t = (q.get("text") or q.get("quote") or "").strip().strip('"').strip("'").strip()
        if not t:
            return None
        d = {"text": t}
        style = (q.get("style") or "").strip()
        style = LEGACY_QUOTE_STYLE_MAP.get(style, style)
        if style in VALID_QUOTE_STYLES:
            d["style"] = style
        punch = (q.get("punch") or "").strip()
        if punch and punch in t:
            d["punch"] = punch
        return d
    return None


def dedupe_quotes(quotes):
    """
    Drop duplicates and near-duplicates. Marker clips overlap the full sermon
    transcript, so the same quote often arrives from 2-3 Claude calls. We normalize
    to lowercase alphanumerics, then suppress any quote whose normalized form is
    a substring of a quote we've already kept (or vice versa).

    Accepts both legacy bare strings and the new {text, style, punch} schema.
    Returns a list of dicts (always {text, ...}) ready for make_quote_images.py.
    """
    cleaned = []
    for q in quotes:
        d = _quote_to_dict(q)
        if not d:
            continue
        if 8 <= len(d["text"]) <= 280:
            cleaned.append(d)

    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    kept = []
    kept_norms = []
    for d in cleaned:
        n = norm(d["text"])
        if not n:
            continue
        dup = False
        for i, kn in enumerate(kept_norms):
            if n == kn or n in kn or kn in n:
                # Keep the longer of the two (more complete thought); preserve
                # style / punch metadata from whichever entry we keep.
                if len(d["text"]) > len(kept[i]["text"]):
                    kept[i] = d
                    kept_norms[i] = n
                dup = True
                break
        if not dup:
            kept.append(d)
            kept_norms.append(n)
    return kept


def overlaps(abs_start, abs_end, accepted, threshold=0.5):
    """Return True if [abs_start, abs_end] overlaps >threshold of either interval."""
    dur = abs_end - abs_start
    for a_start, a_end in accepted:
        overlap = max(0, min(abs_end, a_end) - max(abs_start, a_start))
        if overlap / max(dur, 1) > threshold or overlap / max(a_end - a_start, 1) > threshold:
            return True
    return False


def main():
    transcript_files = sorted(glob.glob(os.path.join(TRANSCRIPTS_DIR, "*_marker_*.json")))

    if not transcript_files:
        print(f"No transcripts found in: {TRANSCRIPTS_DIR}")
        print("Run transcribe.sh first.")
        sys.exit(1)

    print(f"Work dir : {WORK_DIR}")
    print(f"Transcripts: {len(transcript_files)} file(s)")
    print(f"Output   : {CLIPS_DIR}")
    print(f"Mode     : {'EDITED (multi-segment per marker)' if EDITED_MODE else 'simple (single-window per marker)'}")

    all_moments = []
    all_quotes = []  # raw quote strings from every Claude call — deduped later
    all_edited_clips = []  # list of (source_video_path, edited_clip_dict)
    total_cut = 0
    accepted_ranges = []  # (abs_start, abs_end) for cross-clip overlap detection

    for transcript_path in transcript_files:
        stem = os.path.basename(transcript_path).replace(".json", "")
        video_path = os.path.join(WORK_DIR, stem + ".mp4")
        clip_abs_start = marker_abs_start(stem)

        if not os.path.exists(video_path):
            print(f"\n✗ Video not found for transcript: {stem}.mp4")
            continue

        print(f"\n{'='*58}")
        print(f"Source: {stem}")

        with open(transcript_path) as f:
            data = json.load(f)

        segments = data.get("segments", [])
        if not segments:
            print("  No segments in transcript — skipping")
            continue

        duration = get_duration(video_path)
        transcript_text = format_transcript(segments)

        print(f"  {len(segments)} segments | {duration:.0f}s clip")

        if EDITED_MODE:
            # Edited mode: skip simple per-marker cuts entirely. Only ask
            # for multi-segment edited clips and accumulate them for the cut step.
            print("  Asking Claude for edited (multi-segment) clips...")
            edited_clips = ask_claude_edited(transcript_text, stem, duration)
            print(f"  {len(edited_clips)} edited clip(s) identified")
            for ec in edited_clips:
                all_edited_clips.append((video_path, ec))
            continue

        print("  Asking Claude for best moments...")
        moments, quotes = ask_claude(transcript_text, stem, duration)
        all_quotes.extend(quotes)

        if not moments:
            print("  No moments returned")
            continue

        edited_clips = []  # not requested in default mode

        for i, m in enumerate(moments):
            start = float(m.get("start", 0))
            end = float(m.get("end", 0))
            title = m.get("title", f"Moment {i+1}")
            dur = end - start

            # Guard rails
            if dur < 35 or dur > 80:
                print(f"  ⚠ Skip '{title}' — {dur:.0f}s out of range")
                continue
            if start < 0 or end > duration + 10:
                print(f"  ⚠ Skip '{title}' — timestamps out of bounds ({start:.0f}–{end:.0f})")
                continue

            # Skip if content overlaps an already-accepted clip
            abs_start = clip_abs_start + start
            abs_end = clip_abs_start + end
            if overlaps(abs_start, abs_end, accepted_ranges):
                print(f"  ⚠ Skip '{title}' — overlaps with already-accepted clip")
                continue

            fname = safe_filename(title) + ".mp4"
            out = os.path.join(CLIPS_DIR, fname)
            if os.path.exists(out):
                out = os.path.join(CLIPS_DIR, safe_filename(title) + f"_{i+1}.mp4")

            s_fmt = f"{int(start//60)}:{int(start%60):02d}"
            e_fmt = f"{int(end//60)}:{int(end%60):02d}"
            print(f"  [{i+1}] {s_fmt}–{e_fmt} ({dur:.0f}s) — {title}")

            if cut_clip(video_path, start, end, out, video_duration=duration):
                virality_total = m.get("virality_total",
                    m.get("hook_score", 0) + m.get("engagement_score", 0) +
                    m.get("value_score", 0) + m.get("shareability_score", 0))
                print(f"       ✓ {os.path.basename(out)}  [virality: {virality_total}/100]")
                total_cut += 1
                accepted_ranges.append((abs_start, abs_end))
                all_moments.append({
                    "source": stem + ".mp4",
                    "start": start,
                    "end": end,
                    "duration": dur,
                    "title": title,
                    "hook": m.get("hook", ""),
                    "hook_type": m.get("hook_type", "none"),
                    "hook_score": m.get("hook_score", 0),
                    "engagement_score": m.get("engagement_score", 0),
                    "value_score": m.get("value_score", 0),
                    "shareability_score": m.get("shareability_score", 0),
                    "virality_total": virality_total,
                    "why": m.get("why", ""),
                    "teaser_start": m.get("teaser_start"),
                    "teaser_end": m.get("teaser_end"),
                    "file": os.path.basename(out),
                    "vertical_file": None,
                })
            else:
                print(f"       ✗ Cut failed")

    # Process full sermon video if transcript exists
    full_sermon_video = find_full_sermon_video(WORK_DIR)
    if full_sermon_video:
        full_stem = os.path.splitext(os.path.basename(full_sermon_video))[0]
        full_transcript = os.path.join(TRANSCRIPTS_DIR, full_stem + '.json')
        if os.path.exists(full_transcript):
            sermon_moments, sermon_quotes = process_full_sermon(full_sermon_video, full_transcript, CLIPS_DIR)
            all_moments.extend(sermon_moments)
            all_quotes.extend(sermon_quotes)
            total_cut += len(sermon_moments)
        else:
            print(f"\n⚠ Full sermon found ({os.path.basename(full_sermon_video)}) but no transcript.")
            print(f"  Run transcribe.sh to generate: transcripts/{full_stem}.json")

    # Write manifest
    manifest = os.path.join(CLIPS_DIR, "moments.json")
    with open(manifest, "w") as f:
        json.dump(all_moments, f, indent=2)

    # Write deduped quotes manifest
    deduped_quotes = dedupe_quotes(all_quotes)
    quotes_manifest = os.path.join(CLIPS_DIR, "quotes.json")
    with open(quotes_manifest, "w") as f:
        json.dump(deduped_quotes, f, indent=2)

    print(f"\n{'='*58}")
    print(f"✓ Cut {total_cut} clips  →  {CLIPS_DIR}")
    print(f"✓ Manifest: {manifest}")
    print(f"✓ Quotes: {len(deduped_quotes)} unique (from {len(all_quotes)} raw)  →  {quotes_manifest}")

    # Build horizontal highlight reel from top moments' teaser hooks
    # (uses simple-cut moments' teaser timestamps; in --edited mode these come
    # only from the full sermon path. Skip if neither produced any moments.)
    if all_moments:
        make_highlight_reel(all_moments, WORK_DIR, CLIPS_DIR)

    # Cut edited clips into viral_clips/ so make_vertical picks them up
    if EDITED_MODE:
        make_edited_clips(all_edited_clips, CLIPS_DIR)


def make_edited_clips(all_edited_clips, edited_dir):
    """Cut and concatenate multi-segment edited clips from Claude's edited_clips output."""
    import tempfile

    if not all_edited_clips:
        print("\n⚠ No edited clips returned — skipping")
        return

    print(f"\n{'='*58}")
    print(f"Cutting {len(all_edited_clips)} edited clip(s)...")
    success_count = 0

    for source_video, ec in all_edited_clips:
        title = ec.get("title", "Edited Clip")
        segments = ec.get("segments", [])
        why = ec.get("why", "")

        if not segments:
            print(f"  ⚠ No segments for '{title}' — skipping")
            continue

        # Validate segments
        total_dur = sum(float(s["end"]) - float(s["start"]) for s in segments)
        if not (35 <= total_dur <= 80):
            print(f"  ⚠ Skip '{title}' — total {total_dur:.0f}s out of range")
            continue

        fname = "edited_" + safe_filename(title) + ".mp4"
        out_path = os.path.join(edited_dir, fname)
        if os.path.exists(out_path):
            fname = "edited_" + safe_filename(title) + "_2.mp4"
            out_path = os.path.join(edited_dir, fname)

        print(f"\n  [{success_count+1}] {title}  ({total_dur:.0f}s, {len(segments)} segments)")
        print(f"       {why}")

        tmpdir = tempfile.mkdtemp()
        seg_files = []

        for j, seg in enumerate(segments):
            raw_s = float(seg["start"])
            raw_e = float(seg["end"])
            if raw_e - raw_s < 3:
                continue
            s = max(0.0, raw_s - EDITED_SEG_HEAD_PAD)
            e = raw_e + EDITED_SEG_TAIL_PAD
            dur = e - s
            seg_path = os.path.join(tmpdir, f"seg_{j:02d}.mp4")
            cmd = [
                "ffmpeg", "-y",
                *HW_ACCEL,
                "-ss", str(s), "-i", source_video,
                "-t", str(dur),
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                seg_path,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and os.path.getsize(seg_path) > 1000:
                seg_files.append(seg_path)
                print(f"       seg {j+1}: {int(s//60)}:{int(s%60):02d}–{int(e//60)}:{int(e%60):02d} ({dur:.0f}s)")
            else:
                print(f"       ✗ seg {j+1} cut failed")

        if not seg_files:
            print(f"       ✗ All segments failed — skipping")
            continue

        list_path = os.path.join(tmpdir, "concat.txt")
        with open(list_path, "w") as f:
            for sf in seg_files:
                f.write(f"file '{sf}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            size_mb = os.path.getsize(out_path) / 1_000_000
            print(f"       ✓ {fname}  ({size_mb:.1f} MB)")
            success_count += 1
        else:
            print(f"       ✗ Concat failed: {r.stderr[-200:]}")

    print(f"\n✓ {success_count}/{len(all_edited_clips)} edited clips  →  {edited_dir}")


if __name__ == "__main__":
    main()
