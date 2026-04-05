# blogpics

Pulls photos from Apple Photos for a given date and creates a WordPress draft post, with each photo in its own Gutenberg gallery block. Posts are created in chronological order (earliest photo first) and titled `[Day] night -` following a long-standing blog convention.

Designed for pre-loading photos before travel, when bandwidth makes uploading painful. After the script runs, you open the draft in WordPress, cull/reorder images, add prose between them, and publish manually.

## Setup

**1. Install dependencies**
```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**2. Create a WordPress Application Password**

In WordPress Admin → Users → Your Profile → scroll to **Application Passwords** → type a name (e.g. `blogpics`) → click Add New → copy the password shown.

> Note: Application Passwords require HTTPS, or add `define('WP_ENVIRONMENT_TYPE', 'local');` to `wp-config.php` to enable them on HTTP sites.

**3. Configure credentials**
```bash
cp .env.example .env
```
Edit `.env` with your WordPress username and the application password you just created.

## Usage

```bash
# Single day
venv/bin/python blogpics.py 2026-04-04

# Date range (one post per day)
venv/bin/python blogpics.py 2026-04-01 2026-04-05

# Force rebuild — deletes existing draft and re-uploads everything
venv/bin/python blogpics.py --force 2026-04-04
```

## Restartable

Progress is saved to `state/YYYY-MM-DD.json` after each successful upload. If the script crashes, re-run the same command and it picks up where it left off — already-uploaded photos are skipped and the existing draft post is updated rather than duplicated.

To start completely fresh for a date, use `--force` or delete `state/YYYY-MM-DD.json`.

## iCloud photos

If your Mac is set to **Optimize Mac Storage** (Photos → Settings → iCloud), originals are stored in iCloud rather than locally. The script handles this automatically by falling back to a Photos app export, which is slower but works. To speed things up, select the photos for your target date in Photos, right-click → **Download** before running the script.

## Settings

These can be changed at the top of `blogpics.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_WIDTH` | `2048` | Max pixel width of uploaded images |
| `JPEG_QUALITY` | `85` | JPEG compression quality (1–95) |
