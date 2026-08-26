#!/usr/bin/env python3
"""
Convert the local Whisper transcript to Defuddle-format markdown and
optionally push it to the prod Episode row in the Cross Church MySQL DB.

Background: Cross Church episodes store their YouTube transcript in
`episodes.transcription_text` as markdown with clickable timestamps
("**0:01** · text..."). `/sync-church-episode` scrapes Defuddle, but
Defuddle can't return anything until YouTube has generated captions
for the video — which can take hours or never happen for unlisted
uploads. Since the sermon-clips pipeline already runs Whisper on the
sermon audio, we already have a perfect word-level transcript locally.
This script formats that transcript and writes it straight to prod.

Workflow — run from the sermon's working directory after transcribe.sh
has completed:

    # Preview only (default): writes transcripts/<stem>.md and shows
    # which prod episode would be updated, but does NOT touch the DB.
    python3 sync_transcript.py

    # Actually push the transcript to prod.
    python3 sync_transcript.py --push

    # Override the matched episode (skip the title fuzzy match):
    python3 sync_transcript.py --push --episode-id 142
    python3 sync_transcript.py --push --title "Authority Over Disease"

    # Skip the y/n confirmation prompt (CI / scripted runs):
    python3 sync_transcript.py --push --no-confirm

Episode matching order:
    1. --episode-id N           (explicit)
    2. --title "exact-ish"      (LIKE %title% with needs-transcript filter)
    3. Local sermon stem        (fuzzy LIKE %stem% against episodes
                                 needing transcript)
    4. Most recent episode      (whose transcription_text is empty or
                                 lacks **MM:SS** markers)

If options 3 and 4 both match the same row, that's the typical happy
path. If they diverge (you're catching up several weeks), use --title
or --episode-id to disambiguate.

Configuration:
    $CROSSCHURCH_DIR — path to the Cross Church Laravel repo.
                        Defaults to ~/Code/crosschurch, falling back to the
                        legacy ~/Code/crosschurch-new.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

def _resolve_crosschurch_dir():
    env = os.environ.get("CROSSCHURCH_DIR")
    if env:
        return os.path.expanduser(env)
    # The repo was renamed crosschurch-new → crosschurch; try both.
    candidates = [
        os.path.expanduser("~/Code/crosschurch"),
        os.path.expanduser("~/Code/crosschurch-new"),
    ]
    for path in candidates:
        if os.path.isfile(os.path.join(path, "artisan")):
            return path
    return candidates[0]


CROSSCHURCH_DIR = _resolve_crosschurch_dir()


# ─── Find the full sermon transcript ────────────────────────────────────────
def find_full_sermon_transcript(work_dir):
    """Return (json_path, stem) for the full sermon transcript in <work_dir>.

    Same logic as find_moments.find_full_sermon_video: any transcript whose
    stem is not a marker clip and not an OBS recording. When several qualify,
    pick the one with the most segments (= longest = full sermon).
    """
    tdir = os.path.join(work_dir, "transcripts")
    if not os.path.isdir(tdir):
        return None, None

    candidates = []
    for fn in os.listdir(tdir):
        if not fn.endswith(".json"):
            continue
        if "_marker_" in fn:
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}", fn):
            continue  # OBS recording
        candidates.append(os.path.join(tdir, fn))

    if not candidates:
        return None, None

    best, best_count = None, 0
    for p in candidates:
        try:
            with open(p) as f:
                data = json.load(f)
            segs = data if isinstance(data, list) else data.get("segments", [])
            if len(segs) > best_count:
                best_count = len(segs)
                best = p
        except (OSError, json.JSONDecodeError):
            continue

    if not best:
        return None, None
    stem = os.path.splitext(os.path.basename(best))[0]
    return best, stem


# ─── Format conversion ───────────────────────────────────────────────────────
def fmt_time(seconds):
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def whisper_to_defuddle(json_path):
    """Convert a Whisper transcript JSON to Defuddle-style markdown."""
    with open(json_path) as f:
        data = json.load(f)
    segs = data if isinstance(data, list) else data.get("segments", [])
    lines = []
    for seg in segs:
        start = seg.get("start")
        if start is None:
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"**{fmt_time(start)}** · {text}")
    return "\n\n".join(lines) + "\n"


# ─── Prod DB access via Laravel Tinker ───────────────────────────────────────
PROD_CONNECTION_BOOT = r"""
$u = parse_url(env('PROD_DB_URL'));
config(['database.connections.prod' => [
    'driver' => 'mysql',
    'host' => $u['host'],
    'port' => $u['port'] ?? 3306,
    'database' => 'main',
    'username' => $u['user'],
    'password' => $u['pass'],
    'charset' => 'utf8mb4',
    'collation' => 'utf8mb4_unicode_ci',
    'prefix' => '',
]]);
"""


def run_tinker(php_code):
    """Run a tinker script in CROSSCHURCH_DIR; return (stdout, stderr, rc)."""
    if not os.path.isdir(CROSSCHURCH_DIR):
        return "", f"CROSSCHURCH_DIR not found: {CROSSCHURCH_DIR}", 2

    full = PROD_CONNECTION_BOOT + "\n" + php_code
    cmd = ["php", "artisan", "tinker", "--execute", full]
    r = subprocess.run(cmd, cwd=CROSSCHURCH_DIR,
                       capture_output=True, text=True, timeout=120)
    return r.stdout, r.stderr, r.returncode


def php_single_quoted(value):
    """Return `value` as a safe PHP single-quoted string literal (quotes
    included). Escapes backslashes and single quotes so paths/titles that
    contain an apostrophe — e.g. "god's failing people.md" — don't break the
    generated PHP."""
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def find_target_episode(stem_hint=None, title=None, episode_id=None):
    """Resolve which prod episode to update. Returns a dict or None."""
    if episode_id:
        php = (
            f"$ep = App\\Models\\Episode::on('prod')->find({int(episode_id)});\n"
            "if ($ep) { echo json_encode([\n"
            "    'id' => $ep->id,\n"
            "    'title' => $ep->title,\n"
            "    'youtube_video_id' => $ep->youtube_video_id,\n"
            "    'youtube_published_at' => (string) $ep->youtube_published_at,\n"
            "    'has_transcript' => !empty($ep->transcription_text),\n"
            "    'has_timestamps' => (bool) preg_match('/\\*\\*\\d+:\\d+\\*\\*/', (string) $ep->transcription_text),\n"
            "    'transcript_len' => strlen((string) $ep->transcription_text),\n"
            "]); } else { echo 'NONE'; }\n"
        )
    else:
        # Build the needs-transcript filter, optionally constrained by title hint.
        needs_filter = (
            "->where(function($q) { $q->whereNull('transcription_text')"
            "->orWhere('transcription_text', '')"
            "->orWhereRaw(\"transcription_text NOT LIKE '%**%:%**%'\"); })"
        )
        if title or stem_hint:
            # Truncate before escaping so a cut can't sever a "\'" escape.
            t = (title or stem_hint or "").strip()[:120]
            t = t.replace("\\", "\\\\").replace("'", "\\'")
            title_clause = f"->where('title', 'like', '%{t}%')"
        else:
            title_clause = ""

        php = (
            "$ep = App\\Models\\Episode::on('prod')"
            "->whereNotNull('youtube_video_id')"
            "->where('youtube_video_id', '!=', '')"
            f"{title_clause}{needs_filter}"
            "->orderByDesc('youtube_published_at')->first();\n"
            "if ($ep) { echo json_encode([\n"
            "    'id' => $ep->id,\n"
            "    'title' => $ep->title,\n"
            "    'youtube_video_id' => $ep->youtube_video_id,\n"
            "    'youtube_published_at' => (string) $ep->youtube_published_at,\n"
            "    'has_transcript' => !empty($ep->transcription_text),\n"
            "    'has_timestamps' => (bool) preg_match('/\\*\\*\\d+:\\d+\\*\\*/', (string) $ep->transcription_text),\n"
            "    'transcript_len' => strlen((string) $ep->transcription_text),\n"
            "]); } else { echo 'NONE'; }\n"
        )

    out, err, rc = run_tinker(php)
    if rc != 0:
        print(f"  ✗ tinker error (rc={rc}): {err.strip()[:300]}", file=sys.stderr)
        return None

    out = out.strip()
    if "NONE" in out and "{" not in out:
        return None

    # Tinker output can include other noise; locate the JSON object
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        print(f"  ✗ no JSON in tinker output:\n{out[:400]}", file=sys.stderr)
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        print(f"  ✗ JSON parse error in tinker output:\n{out[:400]}", file=sys.stderr)
        return None


def push_transcript(episode_id, md_path):
    """Push the transcript file contents to the prod episode."""
    php = (
        f"$ep = App\\Models\\Episode::on('prod')->find({int(episode_id)});\n"
        f"if (!$ep) {{ echo 'EPISODE_NOT_FOUND'; exit; }}\n"
        f"$ep->transcription_text = file_get_contents({php_single_quoted(md_path)});\n"
        "$ep->save();\n"
        "echo json_encode([\n"
        "    'saved' => true,\n"
        "    'id' => $ep->id,\n"
        "    'title' => $ep->title,\n"
        "    'transcript_len' => strlen((string) $ep->transcription_text),\n"
        "    'has_timestamps' => (bool) preg_match('/\\*\\*\\d+:\\d+\\*\\*/', (string) $ep->transcription_text),\n"
        "]);\n"
    )
    out, err, rc = run_tinker(php)
    if rc != 0:
        return False, err.strip()[:400]
    if "EPISODE_NOT_FOUND" in out:
        return False, f"Episode {episode_id} not found on prod"
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        return False, f"Unexpected tinker output:\n{out[:400]}"
    try:
        return True, json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, f"JSON parse error:\n{out[:400]}"


def generate_summary(episode_id):
    """Fill in the episode's ai_summary IF MISSING, using the site's own
    YouTubeService::generateSummaryOnly — the same generator the normal
    Defuddle / scheduled web-side sync uses. The scheduled sync remains the
    source of truth (it selects episodes `whereNull('ai_summary')`); this is a
    fill-the-gap safety net for when we push the transcript directly here before
    that sync has run. If a summary already exists it is left untouched.
    Returns (ok, result_dict_or_error); result has 'existed' True when skipped."""
    php = (
        f"$ep = App\\Models\\Episode::on('prod')->find({int(episode_id)});\n"
        f"if (!$ep) {{ echo 'EPISODE_NOT_FOUND'; exit; }}\n"
        "if (empty($ep->transcription_text)) { echo 'NO_TRANSCRIPT'; exit; }\n"
        "if (!empty($ep->ai_summary)) {\n"
        "    echo json_encode(['existed' => true,\n"
        "        'summary_len' => strlen((string) $ep->ai_summary),\n"
        "        'summary' => (string) $ep->ai_summary]);\n"
        "    exit;\n"
        "}\n"
        "(new App\\Services\\YouTubeService())->generateSummaryOnly($ep);\n"
        f"$ep2 = App\\Models\\Episode::on('prod')->find({int(episode_id)});\n"
        "echo json_encode([\n"
        "    'existed' => false,\n"
        "    'summary_len' => strlen((string) $ep2->ai_summary),\n"
        "    'summary' => (string) $ep2->ai_summary,\n"
        "]);\n"
    )
    # Parse the output BEFORE trusting the exit code: tinker returns rc=1
    # whenever the PHP calls exit(), which our early-return branches do even on
    # success (e.g. the "already exists" skip). So sentinels/JSON win.
    out, err, rc = run_tinker(php)
    if "EPISODE_NOT_FOUND" in out:
        return False, f"Episode {episode_id} not found on prod"
    if "NO_TRANSCRIPT" in out:
        return False, "Episode has no transcript to summarize"
    m = re.search(r"\{[\s\S]*\}", out)
    if m:
        try:
            return True, json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return False, (err.strip() or out.strip() or f"tinker rc={rc}")[:400]


def generate_study_guide(episode_id):
    """Fill in the episode's study_guide IF MISSING, using the site's own
    YouTubeService::generateStudyGuideOnly — the same generator the normal
    episode-enhancement / scheduled web-side sync uses. The scheduled sync stays
    the source of truth; this is a fill-the-gap safety net for when we push the
    transcript directly here before that sync has run. If a study guide already
    exists it is left untouched. Returns (ok, result_dict_or_error); result has
    'existed' True when skipped.

    Note: generateStudyGuideOnly optionally enriches with a matched SermonNote.
    That lookup resolves on the default DB connection (not 'prod'), so over this
    cross-connection tinker it simply finds no note and degrades to a
    transcript-only guide — non-destructive. The save() still targets prod
    because the episode was loaded via ::on('prod')."""
    php = (
        f"$ep = App\\Models\\Episode::on('prod')->find({int(episode_id)});\n"
        f"if (!$ep) {{ echo 'EPISODE_NOT_FOUND'; exit; }}\n"
        "if (empty($ep->transcription_text)) { echo 'NO_TRANSCRIPT'; exit; }\n"
        "if (is_array($ep->study_guide) && count($ep->study_guide) > 0) {\n"
        "    echo json_encode(['existed' => true,\n"
        "        'has_guide' => true,\n"
        "        'sections' => array_keys($ep->study_guide)]);\n"
        "    exit;\n"
        "}\n"
        "(new App\\Services\\YouTubeService())->generateStudyGuideOnly($ep);\n"
        f"$ep2 = App\\Models\\Episode::on('prod')->find({int(episode_id)});\n"
        "$sg = $ep2->study_guide;\n"
        "echo json_encode([\n"
        "    'existed' => false,\n"
        "    'has_guide' => is_array($sg) && count($sg) > 0,\n"
        "    'sections' => is_array($sg) ? array_keys($sg) : [],\n"
        "]);\n"
    )
    # Parse output before trusting rc (tinker exit() → rc=1 even on the
    # successful "already exists" skip). See generate_summary for detail.
    out, err, rc = run_tinker(php)
    if "EPISODE_NOT_FOUND" in out:
        return False, f"Episode {episode_id} not found on prod"
    if "NO_TRANSCRIPT" in out:
        return False, "Episode has no transcript to build a study guide from"
    m = re.search(r"\{[\s\S]*\}", out)
    if m:
        try:
            return True, json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return False, (err.strip() or out.strip() or f"tinker rc={rc}")[:400]


# ─── Main ────────────────────────────────────────────────────────────────────
def parse_args():
    ap = argparse.ArgumentParser(
        description="Convert local Whisper transcript to Defuddle markdown and push to prod."
    )
    ap.add_argument("work_dir", nargs="?", default=None,
                    help="Sermon working directory (defaults to CWD)")
    ap.add_argument("--push", action="store_true",
                    help="Actually update prod. Default is dry-run (write .md only).")
    ap.add_argument("--episode-id", type=int,
                    help="Force a specific prod episode ID")
    ap.add_argument("--title",
                    help="Fuzzy-match prod episode title (overrides the auto stem-based match)")
    ap.add_argument("--no-confirm", action="store_true",
                    help="Skip the y/n confirmation prompt before pushing")
    ap.add_argument("--no-summary", action="store_true",
                    help="Skip auto-generating the episode ai_summary after the push")
    ap.add_argument("--no-study-guide", action="store_true",
                    help="Skip auto-generating the episode study_guide after the push")
    ap.add_argument("--stdout", action="store_true",
                    help="Print the Defuddle markdown to stdout instead of writing a file")
    return ap.parse_args()


def main():
    args = parse_args()
    work_dir = args.work_dir or os.getcwd()

    # 1. Find local transcript
    json_path, stem = find_full_sermon_transcript(work_dir)
    if not json_path:
        print(f"✗ No full-sermon transcript found in {work_dir}/transcripts/")
        print("  Run transcribe.sh first.")
        sys.exit(1)

    print(f"Local    : {json_path}")
    md = whisper_to_defuddle(json_path)
    seg_count = md.count("\n\n") + (1 if md else 0)
    print(f"Format   : {len(md):,} chars  /  ~{seg_count} segments")

    # 2. Write the .md
    if args.stdout:
        sys.stdout.write(md)
        sys.stdout.flush()
        return

    md_path = os.path.join(work_dir, "transcripts", stem + ".md")
    with open(md_path, "w") as f:
        f.write(md)
    print(f"Wrote    : {md_path}")

    # 3. Find target episode
    if not args.push:
        # Still do a dry-run match for visibility
        target = find_target_episode(stem_hint=stem, title=args.title,
                                     episode_id=args.episode_id)
        if target:
            print("\nWould push to (dry run — pass --push to commit):")
            _print_episode(target)
        else:
            print("\n⚠ No matching prod episode found needing a transcript.")
            print("  Pass --episode-id <id> or --title to override.")
        return

    target = find_target_episode(stem_hint=stem, title=args.title,
                                 episode_id=args.episode_id)
    if not target:
        print("✗ No matching prod episode. Use --episode-id <id> or --title.")
        sys.exit(1)

    print("\nTarget episode:")
    _print_episode(target)

    if not args.no_confirm:
        print("")
        try:
            answer = input("Push transcript to this episode? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    ok, result = push_transcript(target["id"], md_path)
    if not ok:
        print(f"\n✗ Push failed: {result}")
        sys.exit(1)
    print(f"\n✓ Saved to prod episode {result['id']}: {result['title']}")
    print(f"  Transcript length: {result['transcript_len']:,} chars")
    print(f"  Has timestamps:    {'yes' if result['has_timestamps'] else 'no'}")

    # 4. Fill in the ai_summary and study_guide IF MISSING. The scheduled
    #    web-side sync is the source of truth and only fills empty fields; we
    #    mirror that here as a safety net, because pushing the transcript
    #    directly can run before that sync. Existing values are never clobbered.
    if not args.no_summary:
        print("\n▶ Checking episode summary...")
        ok, sresult = generate_summary(result["id"])
        if not ok:
            print(f"  ⚠ Summary skipped: {sresult}")
            print("    (transcript is saved; re-run to retry, or --no-summary to suppress)")
        elif sresult.get("existed"):
            print(f"  ✓ ai_summary already present ({sresult['summary_len']:,} chars) — left as-is")
        elif sresult.get("summary_len", 0) > 0:
            print(f"  ✓ ai_summary generated ({sresult['summary_len']:,} chars)")
            print(f"    {sresult['summary'][:200]}"
                  + ("…" if len(sresult.get("summary", "")) > 200 else ""))
        else:
            print("  ⚠ Summary generator returned empty — left ai_summary unchanged")

    if not args.no_study_guide:
        print("\n▶ Checking episode study guide...")
        ok, gresult = generate_study_guide(result["id"])
        if not ok:
            print(f"  ⚠ Study guide skipped: {gresult}")
            print("    (transcript is saved; re-run to retry, or --no-study-guide to suppress)")
        elif gresult.get("existed"):
            print(f"  ✓ study_guide already present ({len(gresult.get('sections', []))} sections) — left as-is")
        elif gresult.get("has_guide"):
            print(f"  ✓ study_guide generated ({len(gresult.get('sections', []))} sections: "
                  f"{', '.join(gresult.get('sections', []))})")
        else:
            print("  ⚠ Study guide generator returned empty — left study_guide unchanged")


def _print_episode(ep):
    badge = " ✓ has timestamps" if ep.get("has_timestamps") else (
        " · existing plain text" if ep.get("has_transcript") else " · no transcript")
    print(f"  id     : {ep['id']}")
    print(f"  title  : {ep['title']}")
    print(f"  yt id  : {ep.get('youtube_video_id', '')}")
    print(f"  published: {ep.get('youtube_published_at', '')}")
    print(f"  current: {ep.get('transcript_len', 0):,} chars{badge}")


if __name__ == "__main__":
    main()
