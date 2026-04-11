#!/usr/bin/env python3
"""
blogpics.py — Pull Apple Photos for a date and create a WordPress draft post.

Usage:
    python blogpics.py 2026-04-04                   # single day
    python blogpics.py 2026-04-01 2026-04-05        # inclusive date range
    python blogpics.py --force 2026-04-04           # delete & recreate from scratch
    python blogpics.py --catchup 2026-04-10         # process from last uploaded date up to given date
    python blogpics.py --today                      # process from last uploaded date up to today

Photos are inserted in chronological order (earliest first).
Each photo gets its own Gutenberg gallery block.
Post is created as a draft, dated to the photo date, with a blank title.

Restartable: state is saved to state/YYYY-MM-DD.json after each upload.
Re-running the same date skips already-uploaded photos and updates the post.
"""

import sys
import os
import json
import tempfile
import base64
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
from PIL import Image, ImageOps
from dotenv import load_dotenv
import osxphotos

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # HEIC support optional; install pillow-heif if you see HEIC errors

load_dotenv()

WP_BASE      = os.getenv("WP_BASE", "http://johncohn.org/base").rstrip("/")
WP_USER      = os.getenv("WP_USER")
WP_PASS      = os.getenv("WP_APP_PASSWORD")
MAX_WIDTH    = 2048
JPEG_QUALITY = 85
STATE_DIR    = Path(__file__).parent / "state"


# ---------------------------------------------------------------------------
# State helpers  (state/YYYY-MM-DD.json)
# ---------------------------------------------------------------------------
# Schema:
#   {
#     "post_id": 123 | null,
#     "photos": {
#       "<photo_uuid>": {"id": 456, "url": "https://..."}
#     }
#   }

def state_path(target_date: date) -> Path:
    STATE_DIR.mkdir(exist_ok=True)
    return STATE_DIR / f"{target_date.strftime('%Y-%m-%d')}.json"


def load_state(target_date: date) -> dict:
    p = state_path(target_date)
    if p.exists():
        return json.loads(p.read_text())
    return {"post_id": None, "photos": {}}


def save_state(target_date: date, state: dict):
    state_path(target_date).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# WordPress helpers
# ---------------------------------------------------------------------------

def auth_headers():
    token = base64.b64encode(f"{WP_USER}:{WP_PASS}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def upload_image(file_path: str, filename: str) -> tuple[int, str]:
    """Upload a local image to the WP media library.
    Returns (attachment_id, source_url).
    """
    headers = {
        **auth_headers(),
        "Content-Type": "image/jpeg",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    with open(file_path, "rb") as fh:
        resp = requests.post(f"{WP_BASE}/wp-json/wp/v2/media", headers=headers, data=fh)
    resp.raise_for_status()
    data = resp.json()
    return data["id"], data["source_url"]


def delete_post(post_id: int):
    """Move a post to trash."""
    resp = requests.delete(
        f"{WP_BASE}/wp-json/wp/v2/posts/{post_id}",
        headers=auth_headers(),
    )
    resp.raise_for_status()


def create_draft(blocks: list[str], post_date: date) -> tuple[int, str]:
    """Create a new draft post. Returns (post_id, edit_url)."""
    date_str = datetime(post_date.year, post_date.month, post_date.day, 12, 0, 0).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    day_name = datetime(post_date.year, post_date.month, post_date.day).strftime("%A")
    payload = {
        "title":   f"{day_name} night -",
        "content": "\n\n".join(blocks),
        "status":  "draft",
        "date":    date_str,
    }
    headers = {**auth_headers(), "Content-Type": "application/json"}
    resp = requests.post(f"{WP_BASE}/wp-json/wp/v2/posts", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    post_id = data["id"]
    return post_id, f"{WP_BASE}/wp-admin/post.php?post={post_id}&action=edit"


def update_draft(post_id: int, blocks: list[str]) -> str:
    """Update an existing draft post with new content. Returns edit_url."""
    payload = {"content": "\n\n".join(blocks)}
    headers = {**auth_headers(), "Content-Type": "application/json"}
    resp = requests.post(f"{WP_BASE}/wp-json/wp/v2/posts/{post_id}", headers=headers, json=payload)
    resp.raise_for_status()
    return f"{WP_BASE}/wp-admin/post.php?post={post_id}&action=edit"


def gallery_block(attachment_id: int, image_url: str) -> str:
    """One Gutenberg gallery block containing a single image."""
    inner = (
        f'<!-- wp:image {{"id":{attachment_id},"sizeSlug":"medium_large","linkDestination":"none"}} -->\n'
        f'<figure class="wp-block-image size-medium_large">'
        f'<img src="{image_url}" alt="" class="wp-image-{attachment_id}"/>'
        f'</figure>\n'
        f'<!-- /wp:image -->'
    )
    return (
        f'<!-- wp:gallery {{"columns":1,"linkTo":"none"}} -->\n'
        f'<figure class="wp-block-gallery has-nested-images columns-1 is-cropped">'
        f'{inner}'
        f'</figure>\n'
        f'<!-- /wp:gallery -->'
    )


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def resize_and_save(src_path: str) -> str:
    """Resize to MAX_WIDTH, correct EXIF rotation, save as JPEG temp file.
    Returns the temp file path — caller must delete it.
    """
    img = Image.open(src_path)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > MAX_WIDTH:
        ratio = MAX_WIDTH / img.width
        img = img.resize((MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    img.save(tmp.name, "JPEG", quality=JPEG_QUALITY)
    return tmp.name


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".tiff", ".tif"}


def process_day(target_date: date, photosdb: osxphotos.PhotosDB, force: bool = False) -> dict:
    """Returns a summary dict with keys: date, uploaded, skipped, failed, post_id."""
    print(f"\n{'='*52}")
    print(f"  {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*52}")

    state = load_state(target_date)

    if force:
        if state["post_id"]:
            print(f"  Deleting existing draft (post {state['post_id']})…")
            try:
                delete_post(state["post_id"])
            except requests.HTTPError as e:
                print(f"  ✗ could not delete post: {e.response.status_code}")
        state = {"post_id": None, "photos": {}}
        save_state(target_date, state)
        print("  State cleared — uploading all photos fresh")

    already_done = state["photos"]
    if already_done and not force:
        print(f"  Resuming — {len(already_done)} photo(s) already uploaded")

    start  = datetime(target_date.year, target_date.month, target_date.day,  0,  0,  0)
    end    = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
    photos = photosdb.photos(from_date=start, to_date=end)
    photos = [p for p in photos if not p.hidden and not p.intrash and not p.ismovie]
    photos.sort(key=lambda p: p.date)

    if not photos:
        print("  No photos found — skipping.")
        return {"date": target_date, "uploaded": 0, "skipped": 0, "failed": [], "post_id": None}

    print(f"  Found {len(photos)} photo(s)")

    skipped  = []   # already done in a prior run
    uploaded = []   # succeeded this run
    failed   = []   # failed this run

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, photo in enumerate(photos, 1):
            time_str = photo.date.strftime("%H:%M:%S")
            uuid = photo.uuid

            if uuid in already_done:
                print(f"  [{i:>3}/{len(photos)}] {photo.original_filename}  ({time_str})  — skipping (already uploaded)")
                skipped.append(photo.original_filename)
                continue

            print(f"  [{i:>3}/{len(photos)}] {photo.original_filename}  ({time_str})", end="", flush=True)

            fail_reason = None

            try:
                exported = photo.export(tmpdir, overwrite=True, use_photos_export=False)
                if not exported:
                    print("  (fetching from iCloud…)", end="", flush=True)
                    try:
                        exported = photo.export(tmpdir, overwrite=True, use_photos_export=True)
                    except Exception as e:
                        fail_reason = f"iCloud export failed: {e}"
            except Exception as e:
                fail_reason = f"export failed: {e}"

            if not fail_reason and not exported:
                fail_reason = "could not retrieve from iCloud"

            if not fail_reason:
                image_files = [f for f in exported if Path(f).suffix.lower() in IMAGE_EXTS]
                if not image_files:
                    fail_reason = "no image file in export"

            if not fail_reason:
                try:
                    resized = resize_and_save(image_files[0])
                except Exception as e:
                    fail_reason = f"resize failed: {e}"
                    resized = None

            if not fail_reason:
                try:
                    filename = f"{target_date.strftime('%Y%m%d')}_{i:03d}.jpg"
                    att_id, att_url = upload_image(resized, filename)
                    state["photos"][uuid] = {"id": att_id, "url": att_url}
                    save_state(target_date, state)
                    uploaded.append(photo.original_filename)
                    print(f"  ✓  id={att_id}")
                except Exception as e:
                    fail_reason = f"upload failed: {e}"
                finally:
                    if resized and os.path.exists(resized):
                        os.unlink(resized)

            if fail_reason:
                print(f"  ✗ {fail_reason}")
                failed.append((photo.original_filename, fail_reason))

    # Build blocks in original photo order
    blocks = []
    for photo in photos:
        entry = state["photos"].get(photo.uuid)
        if entry:
            blocks.append(gallery_block(entry["id"], entry["url"]))

    # Summary
    print(f"\n  Results: {len(uploaded)} uploaded, {len(skipped)} skipped, {len(failed)} failed")
    if failed:
        print("  Failed photos:")
        for name, reason in failed:
            print(f"    ✗ {name}: {reason}")

    if not blocks:
        print("  No images to post — skipping post creation.")
        return {"date": target_date, "uploaded": len(uploaded), "skipped": len(skipped), "failed": failed, "post_id": None}

    try:
        if state["post_id"]:
            edit_url = update_draft(state["post_id"], blocks)
            print(f"  Draft updated ({len(blocks)} image(s))")
        else:
            post_id, edit_url = create_draft(blocks, target_date)
            state["post_id"] = post_id
            save_state(target_date, state)
            print(f"  Draft created ({len(blocks)} image(s))")
        print(f"  Edit here: {edit_url}")
    except Exception as e:
        print(f"  ✗ post save failed: {e}")
        print("  Uploaded images are saved — re-run without --force to retry the post.")

    return {"date": target_date, "uploaded": len(uploaded), "skipped": len(skipped), "failed": failed, "post_id": state["post_id"]}


def find_last_processed_date() -> date | None:
    """Return the most recent date that has a completed state file (post_id set)."""
    if not STATE_DIR.exists():
        return None
    candidates = []
    for f in STATE_DIR.glob("*.json"):
        try:
            d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            state = json.loads(f.read_text())
            if state.get("post_id"):
                candidates.append(d)
        except Exception:
            continue
    return max(candidates) if candidates else None


def main():
    if not WP_USER or not WP_PASS:
        print("Error: WP_USER and WP_APP_PASSWORD must be set in your .env file.")
        print("See .env.example for the format.")
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    force   = "--force"   in args
    catchup = "--catchup" in args
    today   = "--today"   in args
    args    = [a for a in args if a not in ("--force", "--catchup", "--today")]

    if today or catchup:
        last = find_last_processed_date()
        if not last:
            print("Error: no previously processed dates found in state/. Run a specific date first.")
            sys.exit(1)
        start_date = last + timedelta(days=1)
        if today:
            end_date = date.today()
        else:
            if not args:
                print("Error: --catchup requires a date, e.g. --catchup 2026-04-10")
                sys.exit(1)
            try:
                end_date = datetime.strptime(args[0], "%Y-%m-%d").date()
            except ValueError:
                print("Date format must be YYYY-MM-DD")
                sys.exit(1)
        if start_date > end_date:
            print(f"Already up to date — last processed date was {last}.")
            sys.exit(0)
        print(f"Catching up from {start_date} to {end_date} (last processed: {last})")
    else:
        try:
            start_date = datetime.strptime(args[0], "%Y-%m-%d").date()
            end_date   = datetime.strptime(args[1], "%Y-%m-%d").date() if len(args) > 1 else start_date
        except (ValueError, IndexError):
            print("Date format must be YYYY-MM-DD")
            sys.exit(1)

        if start_date > end_date:
            print("Start date must be on or before end date.")
            sys.exit(1)

    print("Loading Photos library (this may take a moment)…")
    photosdb = osxphotos.PhotosDB()

    summaries = []
    current = start_date
    while current <= end_date:
        result = process_day(current, photosdb, force=force)
        if result:
            summaries.append(result)
        current += timedelta(days=1)

    if len(summaries) > 1:
        print(f"\n{'='*52}")
        print("  OVERALL SUMMARY")
        print(f"{'='*52}")
        total_up = total_sk = total_fa = 0
        for s in summaries:
            status = "ok" if not s["failed"] else f"{len(s['failed'])} failed"
            print(f"  {s['date']}  uploaded={s['uploaded']}  skipped={s['skipped']}  [{status}]")
            total_up += s["uploaded"]
            total_sk += s["skipped"]
            total_fa += len(s["failed"])
        print(f"\n  Total: {total_up} uploaded, {total_sk} skipped, {total_fa} failed across {len(summaries)} day(s)")

    print("\nAll done.")


if __name__ == "__main__":
    main()
