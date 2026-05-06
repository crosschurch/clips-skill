#!/usr/bin/env python3
"""
Upload top-N vertical sermon clips to Descript as one project per clip,
all inside a named folder (default "Cross Church").

Per-clip projects are required because Descript's import API:
  1. Always creates a new project (no append-to-existing)
  2. Returns HTTP 500 when add_media has ≥5 items (undocumented limit)
A single project with all 10 clips is not achievable via the API today.

Project name per clip: "<Session> — NN. <Title>"
(e.g., "Sermon 0419 — 01. Demons Had Better Theology Than Pharisees")
Session is derived from the working directory name (title-cased).

Token: read from $DESCRIPT_API_TOKEN, or sourced from
       ~/.config/sermon-clips/descript.env if present.

Auth format (probed against /v1/status): the token is the full
"dx_bearer_<id>:dx_secret_<secret>" string passed as-is in a Bearer
header.

Per clip:
  1. POST /v1/jobs/import/project_media with 1 media + 1 composition
     → response contains signed upload_urls
  2. PUT the file to its signed URL (Content-Type: application/octet-stream)
  3. Poll GET /v1/jobs/{job_id} until terminal status

Usage:
    python3 upload_to_descript.py              # top 10 from moments.json
    python3 upload_to_descript.py --top 5      # top 5
    python3 upload_to_descript.py --all        # everything in moments.json
    python3 upload_to_descript.py --folder "Other" --session "Custom"
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

API_BASE = "https://descriptapi.com/v1"
DEFAULT_FOLDER = "Cross Church"
DEFAULT_TOP = 10
ENV_FILE = Path.home() / ".config" / "sermon-clips" / "descript.env"


def load_token() -> str:
    tok = os.environ.get("DESCRIPT_API_TOKEN")
    if tok:
        return tok.strip()
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "DESCRIPT_API_TOKEN":
                    return v.strip().strip('"').strip("'")
    sys.exit(
        f"ERROR: no Descript token found.\n"
        f"Set $DESCRIPT_API_TOKEN or create {ENV_FILE} with:\n"
        f"  DESCRIPT_API_TOKEN=dx_bearer_...:dx_secret_..."
    )


def api_request(method: str, path: str, token: str, body: dict | None = None,
                max_429_retries: int = 5) -> dict:
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            # 429 → honor Retry-After, then retry
            if e.code == 429 and attempt < max_429_retries:
                retry_after = int(e.headers.get("Retry-After", "30") or 30)
                attempt += 1
                print(f"  (429 rate-limited, waiting {retry_after}s — attempt {attempt}/{max_429_retries})")
                time.sleep(retry_after + 1)
                continue
            msg = e.read().decode(errors="replace")
            sys.exit(f"ERROR: {method} {url} → HTTP {e.code}\n{msg}")


def project_name_from_cwd() -> str:
    name = Path.cwd().name or "Sermon"
    return re.sub(r"\s+", " ", name).strip().title()


def pick_moments(moments: list[dict], limit: int | None) -> list[dict]:
    ranked = sorted(
        moments,
        key=lambda m: m.get("virality_total", 0),
        reverse=True,
    )
    return ranked if limit is None else ranked[:limit]


def vertical_path_for(moment: dict, vertical_dir: Path) -> Path | None:
    # moments.json records the vertical filename directly; fall back to
    # deriving it from `file` for older manifests.
    name = moment.get("vertical_file")
    if name:
        cand = vertical_dir / name
        if cand.is_file():
            return cand
    stem = Path(moment["file"]).stem
    cand = vertical_dir / f"{stem}_vertical.mp4"
    return cand if cand.is_file() else None


def sanitize_media_key(title: str, idx: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", title).strip("_")
    return f"{idx:02d}_{safe}.mp4"[:120]


def upload_file(local: Path, signed_url: str) -> None:
    # Descript's signed URLs are presigned with Content-Type=application/octet-stream;
    # the PUT Content-Type must match what was signed.
    size = local.stat().st_size
    with local.open("rb") as f:
        req = urllib.request.Request(signed_url, data=f.read(), method="PUT")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(size))
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                r.read()
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:500]
            sys.exit(f"ERROR: PUT {local.name} → HTTP {e.code}\n{msg}")


def poll_job(job_id: str, token: str, poll_every: int = 5, max_wait: int = 1800) -> dict:
    waited = 0
    last_status = None
    while waited < max_wait:
        job = api_request("GET", f"/jobs/{job_id}", token)
        status = job.get("status", "unknown")
        if status != last_status:
            print(f"  job {job_id}: {status}")
            last_status = status
        if status in ("succeeded", "failed", "cancelled", "completed", "error"):
            return job
        time.sleep(poll_every)
        waited += poll_every
    sys.exit(f"ERROR: job {job_id} did not finish within {max_wait}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help=f"Upload top N by virality_total (default {DEFAULT_TOP})")
    ap.add_argument("--all", action="store_true",
                    help="Upload every moment in moments.json")
    ap.add_argument("--folder", default=DEFAULT_FOLDER,
                    help=f'Descript folder path (default "{DEFAULT_FOLDER}")')
    ap.add_argument("--session", default=None,
                    help="Session prefix for project names (default: working dir title-cased)")
    ap.add_argument("--work-dir", default=".",
                    help="Session directory with viral_clips/ and vertical_clips/")
    ap.add_argument("--skip-poll", action="store_true",
                    help="Don't wait for job completion (faster, less confirmation)")
    ap.add_argument("--skip", type=int, default=0,
                    help="Skip the first N picks (resume after partial failure)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan and exit, do not call the API")
    args = ap.parse_args()

    work = Path(args.work_dir).resolve()
    os.chdir(work)

    moments_path = work / "viral_clips" / "moments.json"
    vertical_dir = work / "vertical_clips"
    if not moments_path.is_file():
        sys.exit(f"ERROR: {moments_path} not found — run find_moments.py first")
    if not vertical_dir.is_dir():
        sys.exit(f"ERROR: {vertical_dir} not found — run make_vertical.py first")

    moments = json.loads(moments_path.read_text())
    if not isinstance(moments, list):
        sys.exit("ERROR: moments.json is not a list")

    limit = None if args.all else args.top
    picks = pick_moments(moments, limit)

    plan: list[tuple[int, Path, dict]] = []
    skipped: list[dict] = []
    for idx, m in enumerate(picks, start=1):
        if idx <= args.skip:
            continue
        vpath = vertical_path_for(m, vertical_dir)
        if vpath is None:
            skipped.append(m)
            continue
        plan.append((idx, vpath, m))

    if not plan:
        sys.exit("ERROR: no vertical clips matched moments.json picks")

    session = args.session or project_name_from_cwd()
    print(f"Session : {session}")
    print(f"Folder  : {args.folder}")
    print(f"Clips   : {len(plan)} ({'all' if args.all else f'top {args.top}'})")
    print(f"Mode    : one Descript project per clip (API limit: ≤4 media per import)")
    for idx, vpath, m in plan:
        size_mb = vpath.stat().st_size / 1_048_576
        print(f"  · [{m.get('virality_total','?'):>3}] {idx:02d}. {m.get('title','?')}  ({size_mb:.1f} MB)")
    if skipped:
        print(f"Skipped (no vertical file): {len(skipped)}")
        for m in skipped:
            print(f"  · {m.get('title','?')}  (expected {Path(m['file']).stem}_vertical.mp4)")

    if args.dry_run:
        print("\n[dry-run] would POST one import per clip and PUT the file to each signed URL")
        return 0

    token = load_token()

    results: list[dict] = []
    failures: list[dict] = []
    # Descript rate-limit is ~10 req/min. Space imports ~6.5s apart.
    throttle_seconds = 6.5
    for i, (idx, vpath, m) in enumerate(plan):
        if i > 0:
            time.sleep(throttle_seconds)
        _ = i  # quiet unused-index warning; loop var keeps original tuple
        title = m.get("title", vpath.stem)
        project_name = f"{session} — {idx:02d}. {title}"[:200]
        media_key = sanitize_media_key(title, idx)
        size = vpath.stat().st_size
        size_mb = size / 1_048_576
        payload = {
            "project_name": project_name,
            "folder_name": args.folder,
            "team_access": "edit",
            "add_media": {
                media_key: {"content_type": "video/mp4", "file_size": size},
            },
            "add_compositions": [
                {"name": title[:120], "clips": [{"media": media_key}]},
            ],
        }

        print(f"\n[{idx}/{len(plan)}] {project_name}")
        try:
            resp = api_request("POST", "/jobs/import/project_media", token, payload)
        except SystemExit as e:
            print(f"  ✗ import failed: {e}")
            failures.append({"title": title, "stage": "import", "error": str(e)})
            continue

        job_id = resp.get("job_id")
        project_url = resp.get("project_url")
        upload_urls = resp.get("upload_urls") or {}
        entry = upload_urls.get(media_key)
        if not (job_id and entry):
            print(f"  ✗ unexpected import response: {json.dumps(resp)[:500]}")
            failures.append({"title": title, "stage": "import", "error": "bad response"})
            continue
        signed = entry["upload_url"] if isinstance(entry, dict) else entry
        print(f"  → upload ({size_mb:.1f} MB)...")
        try:
            upload_file(vpath, signed)
        except SystemExit as e:
            print(f"  ✗ upload failed: {e}")
            failures.append({"title": title, "stage": "upload", "error": str(e), "project_url": project_url})
            continue

        status = "submitted"
        if not args.skip_poll:
            try:
                final = poll_job(job_id, token, poll_every=5, max_wait=600)
                status = final.get("status", "unknown")
            except SystemExit as e:
                print(f"  ⚠ poll failed: {e}")
                status = "poll-timeout"

        print(f"  ✓ {status}  {project_url or ''}")
        results.append({
            "rank": idx,
            "title": title,
            "project_name": project_name,
            "project_url": project_url,
            "job_id": job_id,
            "status": status,
        })

    manifest_path = work / "descript_uploads.json"
    manifest_path.write_text(json.dumps({
        "session": session,
        "folder": args.folder,
        "results": results,
        "failures": failures,
    }, indent=2))
    print(f"\nWrote manifest: {manifest_path}")
    print(f"Uploaded: {len(results)}/{len(plan)}  Failed: {len(failures)}")
    for r in results:
        print(f"  {r['rank']:02d}. {r['title']}")
        print(f"      {r['project_url']}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
