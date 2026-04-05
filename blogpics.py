#!/usr/bin/env python3
"""
blogpics.py — Pull Apple Photos for a date and create a WordPress draft post.

Usage:
    python blogpics.py 2026-04-04                   # single day
    python blogpics.py 2026-04-01 2026-04-05        # inclusive date range

Photos are inserted in chronological order (earliest first).
Each photo gets its own Gutenberg gallery block.
Post is created as a draft, dated to the photo date, with a blank title.
"""

import sys
import os
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


def create_draft(blocks: list[str], post_date: date) -> tuple[int, str]:
    """Create a draft post containing the given Gutenberg blocks.
    Returns (post_id, edit_url).
    """
    content  = "\n\n".join(blocks)
    date_str = datetime(post_date.year, post_date.month, post_date.day, 12, 0, 0).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    payload = {
        "title":   "",
        "content": content,
        "status":  "draft",
        "date":    date_str,
    }
    headers = {**auth_headers(), "Content-Type": "application/json"}
    resp = requests.post(f"{WP_BASE}/wp-json/wp/v2/posts", headers=headers, json=payload)
    resp.raise_for_status()
    data    = resp.json()
    post_id = data["id"]
    edit_url = f"{WP_BASE}/wp-admin/post.php?post={post_id}&action=edit"
    return post_id, edit_url


def gallery_block(attachment_id: int, image_url: str) -> str:
    """One Gutenberg gallery block containing a single image."""
    inner = (
        f'<!-- wp:image {{"id":{attachment_id},"sizeSlug":"large","linkDestination":"none"}} -->\n'
        f'<figure class="wp-block-image size-large">'
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
    img = ImageOps.exif_transpose(img)   # fix phone rotation
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

def process_day(target_date: date, photosdb: osxphotos.PhotosDB):
    print(f"\n{'='*52}")
    print(f"  {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*52}")

    start  = datetime(target_date.year, target_date.month, target_date.day,  0,  0,  0)
    end    = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)
    photos = photosdb.photos(from_date=start, to_date=end)
    photos = [p for p in photos if not p.hidden and not p.intrash and not p.ismovie]
    photos.sort(key=lambda p: p.date)   # chronological — earliest first

    if not photos:
        print("  No photos found — skipping.")
        return

    print(f"  Found {len(photos)} photo(s)")

    blocks = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, photo in enumerate(photos, 1):
            time_str = photo.date.strftime("%H:%M:%S")
            print(f"  [{i:>3}/{len(photos)}] {photo.original_filename}  ({time_str})", end="", flush=True)

            try:
                exported = photo.export(tmpdir, overwrite=True, use_photos_export=False)
            except Exception as e:
                print(f"  ✗ export failed: {e}")
                continue

            if not exported:
                print("  ✗ nothing exported (possibly still in iCloud — download it first)")
                continue

            # skip any non-image files (e.g. .mov from live photos)
            image_exts = {".jpg", ".jpeg", ".heic", ".png", ".tiff", ".tif"}
            exported = [f for f in exported if Path(f).suffix.lower() in image_exts]
            if not exported:
                print("  ✗ no image file in export (skipped)")
                continue

            try:
                resized = resize_and_save(exported[0])
            except Exception as e:
                print(f"  ✗ resize failed: {e}")
                continue

            try:
                filename  = f"{target_date.strftime('%Y%m%d')}_{i:03d}.jpg"
                att_id, att_url = upload_image(resized, filename)
                blocks.append(gallery_block(att_id, att_url))
                print(f"  ✓  id={att_id}")
            except requests.HTTPError as e:
                print(f"  ✗ upload failed: {e.response.status_code} {e.response.text[:120]}")
            finally:
                os.unlink(resized)

    if not blocks:
        print("  No images uploaded — skipping post creation.")
        return

    try:
        post_id, edit_url = create_draft(blocks, target_date)
        print(f"\n  Draft created with {len(blocks)} image(s)")
        print(f"  Edit here: {edit_url}")
    except requests.HTTPError as e:
        print(f"  ✗ post creation failed: {e.response.status_code} {e.response.text[:200]}")


def main():
    if not WP_USER or not WP_PASS:
        print("Error: WP_USER and WP_APP_PASSWORD must be set in your .env file.")
        print("See .env.example for the format.")
        sys.exit(1)

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    try:
        start_date = datetime.strptime(args[0], "%Y-%m-%d").date()
        end_date   = datetime.strptime(args[1], "%Y-%m-%d").date() if len(args) > 1 else start_date
    except ValueError:
        print("Date format must be YYYY-MM-DD")
        sys.exit(1)

    if start_date > end_date:
        print("Start date must be on or before end date.")
        sys.exit(1)

    print("Loading Photos library (this may take a moment)…")
    photosdb = osxphotos.PhotosDB()

    current = start_date
    while current <= end_date:
        process_day(current, photosdb)
        current += timedelta(days=1)

    print("\nAll done.")


if __name__ == "__main__":
    main()
