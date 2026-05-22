#!/usr/bin/env python3
"""
Upload the sermon recap to YouTube using YouTube Data API v3.

Reads from <work_dir>/sermon_recap/:
  - recap.mp4         (video file)
  - title.txt         (title — one line)
  - description.txt   (description — full body)
  - thumbnail.jpg     (1280x720 thumbnail)
  - manifest.json     (used as a fallback source for title/description)

Writes <work_dir>/sermon_recap/youtube.json with the uploaded video ID + URL.

Default privacy: unlisted (visible to anyone with the link, not in search /
recommendations). Override with --privacy public|private|unlisted.

Auth (one-time setup):
  1. Create a Google Cloud project, enable YouTube Data API v3
  2. OAuth consent screen → External, add your YouTube channel's email as a
     test user (or publish the app)
  3. Create OAuth client (Desktop) → download client_secret_*.json
  4. Move it to: ~/.config/sermon-clips/youtube-client-secrets.json
  5. Run: python3 youtube_upload.py --setup
     A browser opens; sign in with the channel's Google account; grants
     youtube.upload scope. A refresh token is saved to
     ~/.config/sermon-clips/youtube-token.json

Subsequent uploads use the cached refresh token — no browser needed.

Usage:
    python3 youtube_upload.py [work_dir]                  # upload as unlisted
    python3 youtube_upload.py --privacy private
    python3 youtube_upload.py --playlist PLxxxx           # add to playlist
    python3 youtube_upload.py --dry-run                   # print payload only
    python3 youtube_upload.py --setup                     # one-time OAuth
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    sys.exit(
        "ERROR: missing Google API libraries.\n"
        "Install them with:\n"
        "  pip3 install --user google-api-python-client google-auth-oauthlib google-auth-httplib2"
    )

CONFIG_DIR = Path.home() / ".config" / "sermon-clips"
CLIENT_SECRETS_FILE = CONFIG_DIR / "youtube-client-secrets.json"
TOKEN_FILE = CONFIG_DIR / "youtube-token.json"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CATEGORY_PEOPLE_AND_BLOGS = "22"  # standard for vlog/podcast/sermon recap content
DEFAULT_PRIVACY = "unlisted"


def setup_oauth() -> None:
    if not CLIENT_SECRETS_FILE.is_file():
        sys.exit(
            f"ERROR: client secrets not found at {CLIENT_SECRETS_FILE}\n\n"
            "Setup steps:\n"
            "  1. Go to https://console.cloud.google.com/\n"
            "  2. Create a project (or pick one), enable YouTube Data API v3\n"
            "  3. APIs & Services → OAuth consent screen → External\n"
            "     • Add scope: .../auth/youtube.upload\n"
            "     • Add your channel's Google account as a test user\n"
            "  4. Credentials → Create Credentials → OAuth client ID → Desktop\n"
            "  5. Download the JSON, move it to:\n"
            f"     {CLIENT_SECRETS_FILE}\n"
            "  6. Re-run: python3 youtube_upload.py --setup"
        )

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
    print("Opening browser for YouTube OAuth — sign in with your channel's account...")
    creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    print(f"✓ Saved refresh token → {TOKEN_FILE}")
    print("You can now upload without re-authing.")


def load_credentials() -> Credentials:
    if not TOKEN_FILE.is_file():
        sys.exit(
            f"ERROR: no YouTube refresh token at {TOKEN_FILE}\n"
            "Run one-time setup: python3 youtube_upload.py --setup"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    if not creds.valid:
        sys.exit(
            "ERROR: cached credentials are invalid. Re-run --setup."
        )
    return creds


def read_recap_files(work_dir: Path) -> dict:
    recap_dir = work_dir / "sermon_recap"
    if not recap_dir.is_dir():
        sys.exit(f"ERROR: no sermon_recap/ directory at {recap_dir}\n"
                 "Run make_sermon_recap.py first.")

    video = recap_dir / "recap.mp4"
    if not video.is_file():
        sys.exit(f"ERROR: recap.mp4 not found at {video}")

    title_file = recap_dir / "title.txt"
    desc_file = recap_dir / "description.txt"
    thumb_file = recap_dir / "thumbnail.jpg"
    manifest_file = recap_dir / "manifest.json"

    manifest = {}
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text())

    title = title_file.read_text().strip() if title_file.is_file() else manifest.get("title", "")
    description = desc_file.read_text().strip() if desc_file.is_file() else manifest.get("description", "")

    if not title:
        sys.exit("ERROR: no title found (sermon_recap/title.txt or manifest.json)")
    if len(title) > 100:
        sys.exit(f"ERROR: title is {len(title)} chars; YouTube limit is 100")

    return {
        "video": video,
        "title": title,
        "description": description,
        "thumbnail": thumb_file if thumb_file.is_file() else None,
    }


def upload_video(youtube, video_path: Path, title: str, description: str,
                 privacy: str, dry_run: bool = False) -> str | None:
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": CATEGORY_PEOPLE_AND_BLOGS,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    if dry_run:
        print("DRY RUN — would upload with payload:")
        print(json.dumps(body, indent=2))
        print(f"  video file: {video_path}  ({video_path.stat().st_size / 1e6:.1f} MB)")
        return None

    print(f"Uploading {video_path.name} ({video_path.stat().st_size / 1e6:.1f} MB)...")

    media = MediaFileUpload(
        str(video_path),
        chunksize=8 * 1024 * 1024,  # 8 MB chunks
        resumable=True,
        mimetype="video/mp4",
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    last_progress = -1
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                if pct - last_progress >= 5:
                    print(f"  ...{pct}%")
                    last_progress = pct
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                print(f"  retryable error {e.resp.status}, waiting 5s...")
                time.sleep(5)
                continue
            raise

    video_id = response["id"]
    print(f"✓ Uploaded video: https://www.youtube.com/watch?v={video_id}")
    return video_id


def set_thumbnail(youtube, video_id: str, thumb_path: Path) -> None:
    size_mb = thumb_path.stat().st_size / 1e6
    if size_mb > 2:
        print(f"⚠ Thumbnail is {size_mb:.1f} MB; YouTube limit is 2 MB. Skipping.")
        return
    print(f"Setting thumbnail ({size_mb:.1f} MB)...")
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg"),
    ).execute()
    print("✓ Thumbnail set")


def add_to_playlist(youtube, video_id: str, playlist_id: str) -> None:
    print(f"Adding to playlist {playlist_id}...")
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()
    print("✓ Added to playlist")


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload sermon recap to YouTube")
    parser.add_argument("work_dir", nargs="?", default=os.getcwd(),
                        help="Working directory containing sermon_recap/ (default: cwd)")
    parser.add_argument("--privacy", choices=["public", "unlisted", "private"],
                        default=DEFAULT_PRIVACY,
                        help=f"Video privacy (default: {DEFAULT_PRIVACY})")
    parser.add_argument("--playlist", help="Playlist ID to add the video to (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print upload payload but don't call the API")
    parser.add_argument("--setup", action="store_true",
                        help="One-time OAuth setup (opens browser)")
    args = parser.parse_args()

    if args.setup:
        setup_oauth()
        return

    work_dir = Path(args.work_dir).resolve()
    recap = read_recap_files(work_dir)

    print(f"Title       : {recap['title']}")
    print(f"Description : {len(recap['description'])} chars")
    print(f"Thumbnail   : {recap['thumbnail'].name if recap['thumbnail'] else '(none)'}")
    print(f"Privacy     : {args.privacy}")
    print()

    if args.dry_run:
        upload_video(None, recap["video"], recap["title"], recap["description"],
                     args.privacy, dry_run=True)
        return

    creds = load_credentials()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    video_id = upload_video(youtube, recap["video"], recap["title"],
                            recap["description"], args.privacy)

    if recap["thumbnail"]:
        try:
            set_thumbnail(youtube, video_id, recap["thumbnail"])
        except HttpError as e:
            print(f"⚠ Thumbnail upload failed: {e}")

    if args.playlist:
        try:
            add_to_playlist(youtube, video_id, args.playlist)
        except HttpError as e:
            print(f"⚠ Playlist add failed: {e}")

    out_file = work_dir / "sermon_recap" / "youtube.json"
    out_file.write_text(json.dumps({
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "privacy": args.privacy,
        "title": recap["title"],
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }, indent=2))
    print(f"\n✓ Manifest → {out_file}")
    print(f"\nWatch: https://www.youtube.com/watch?v={video_id}")
    if args.privacy == "unlisted":
        print("Privacy: unlisted — visible to anyone with the link.")
        print("Flip to public in Studio when you're ready to publish.")


if __name__ == "__main__":
    main()
