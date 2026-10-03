#!/usr/bin/env python3
"""
Facebook Photo Downloader Telegram Bot
======================================
Send the bot a Facebook profile link and download photos into a ZIP file.

Usage:
    /fb-pic <profile_link> <amount>  - Download a specific number of photos
    /fb-pic <profile_link>           - Download all available photos
    Or simply send: <profile_link> [amount]
"""

import asyncio
import html
import io
import logging
import os
import random
import re
import shutil
import tempfile
import threading
import zipfile
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FB_COOKIE = os.environ.get("FB_COOKIE", "")  # session cookie from .env

DEFAULT_MAX_PHOTOS = 100   # default when user doesn't specify an amount
ABSOLUTE_MAX_PHOTOS = 300  # upper safety limit per request
MIN_IMAGE_BYTES = 5_000    # skip tiny icons, tracking pixels (< 5KB)
MAX_IMAGE_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Persistent registry of unique 8-digit photo IDs to guarantee no duplicates ever
USED_IDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "used_photo_ids.txt")
_used_ids_lock = threading.Lock()
_used_ids: set[str] = set()


def _init_used_ids() -> None:
    global _used_ids
    if os.path.exists(USED_IDS_FILE):
        try:
            with open(USED_IDS_FILE, "r", encoding="utf-8") as f:
                _used_ids = {line.strip() for line in f if line.strip().isdigit()}
            logger.info("Loaded %d previously used unique photo IDs.", len(_used_ids))
        except Exception as exc:
            logger.warning("Could not load used photo IDs: %s", exc)


_init_used_ids()


def generate_unique_photo_id() -> str:
    """Generate a globally unique 8-digit numeric ID across all downloads and runs."""
    with _used_ids_lock:
        while True:
            candidate = f"{random.randint(10000000, 99999999)}"
            if candidate not in _used_ids:
                _used_ids.add(candidate)
                try:
                    with open(USED_IDS_FILE, "a", encoding="utf-8") as f:
                        f.write(candidate + "\n")
                except Exception as exc:
                    logger.warning("Failed to persist photo ID: %s", exc)
                return candidate

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Brave";v="126"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------
class BotError(Exception):
    """Base error with a user-friendly message."""


class LoginRequiredError(BotError):
    """Facebook demands a login before showing anything."""


class NoPhotosError(BotError):
    """The profile loaded but no downloadable photos were found."""


class FetchError(BotError):
    """The profile page could not be fetched at all."""


# ----------------------------------------------------------------------------
# Facebook scraping
# ----------------------------------------------------------------------------
FB_URL_RE = re.compile(
    r"^(https?://)?(www\.|m\.|mbasic\.)?(facebook\.com|fb\.com)/.+",
    re.IGNORECASE,
)

FB_CDN_RE = re.compile(
    r'(https?:[\\/]+[a-zA-Z0-9\.\-_]*fbcdn\.net[\\/][^"\'\s<>]+)',
    re.IGNORECASE,
)


def is_facebook_url(text: str) -> bool:
    return bool(FB_URL_RE.match(text.strip()))


def profile_slug(url: str) -> str:
    """Derive a safe filename slug from the profile URL path."""
    path = urlparse(url).path.strip("/").split("/")[0]
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", path) or "profile"
    return slug[:40]


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    if FB_COOKIE:
        session.headers.update({"Cookie": FB_COOKIE})
    return session


def clean_img_url(raw: str) -> str | None:
    """Unescape backslashes, HTML entities, and validate image extension."""
    url = re.sub(r"\\+/", "/", raw)
    url = html.unescape(url).strip().rstrip('\\"')
    if "static.xx.fbcdn.net" in url or "/rsrc.php/" in url:
        return None
    base = url.split("?")[0].lower()
    if not base.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return None
    return url


def is_thumbnail_icon(url: str) -> bool:
    """Detect tiny avatar icons (e.g. commenter/friend thumbnails 40x40, 74x74)."""
    m_ctp = re.search(r"ctp=[sp](\d+)x(\d+)", url)
    if m_ctp:
        w, h = int(m_ctp.group(1)), int(m_ctp.group(2))
        if w < 150 and h < 150:
            return True
    return False


def get_resolution_score(url: str) -> int:
    """Estimate image resolution to prefer full-size photos over small thumbnails."""
    m = re.search(r"ctp=[sp](\d+)x(\d+)", url)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"[sp](\d+)x(\d+)", url)
    if m:
        return int(m.group(1)) * int(m.group(2))
    m = re.search(r"cstp=mx(\d+)x(\d+)", url)
    if m:
        return int(m.group(1)) * int(m.group(2))
    if "stp=dst-" in url:
        return 1_000_000
    return 10_000


def extract_photo_urls(html_content: str, page_url: str) -> list[str]:
    """Pull candidate photo URLs out of a Facebook page's HTML."""
    photos_by_id: dict[str, tuple[int, str]] = {}

    soup = BeautifulSoup(html_content, "html.parser")

    # 1. og:image -> usually the profile picture
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        cleaned = clean_img_url(og["content"])
        if cleaned and not is_thumbnail_icon(cleaned):
            fname = cleaned.split("?")[0].split("/")[-1]
            photos_by_id[fname] = (2_000_000, cleaned)

    # 2. Extract fbcdn image URLs
    for match in FB_CDN_RE.finditer(html_content):
        cleaned = clean_img_url(match.group(1))
        if not cleaned or is_thumbnail_icon(cleaned):
            continue
        base = cleaned.split("?")[0].lower()
        fname = base.split("/")[-1]
        score = get_resolution_score(cleaned)
        if fname not in photos_by_id or score > photos_by_id[fname][0]:
            photos_by_id[fname] = (score, cleaned)

    # Sort photos by resolution score descending
    sorted_photos = sorted(photos_by_id.values(), key=lambda x: x[0], reverse=True)
    return [url for _, url in sorted_photos]


def find_album_links(html_content: str) -> list[str]:
    """Extract media/set album URLs from a photos_albums page."""
    albums: set[str] = set()
    # /media/set/?set=a...
    for m in re.findall(r'(\/(?:media\/set|set=a\.)[^\s"\'<>]+)', html_content):
        clean = html.unescape(m.replace(r"\/", "/")).rstrip('\\"')
        if "set=a." in clean:
            full = "https://www.facebook.com" + clean if clean.startswith("/") else clean
            albums.add(full)
    # https://www.facebook.com/media/set/?set=a...
    for m in re.findall(r'(https?:[\\/]+www\.facebook\.com[\\/]+media[\\/]+set[\\/]+[^\s"\'<>]+)', html_content):
        clean = html.unescape(m.replace(r"\/", "/")).rstrip('\\"')
        albums.add(clean)
    return list(albums)


def looks_like_login_wall(html_content: str, final_url: str) -> bool:
    parsed_path = urlparse(final_url).path.lower()
    if "/login" in parsed_path or "checkpoint" in parsed_path:
        return True
    soup = BeautifulSoup(html_content, "html.parser")
    if soup.title and soup.title.string:
        title = soup.title.string.strip().lower()
        if any(marker in title for marker in ["log in", "log into facebook", "login |"]):
            return True
    if soup.find("form", id="login_form") or (soup.find("input", id="email") and soup.find("input", id="pass")):
        return True
    return False


def download_image(session: requests.Session, url: str, dest_path: str) -> bool:
    """Download one image. Returns True on success."""
    try:
        resp = session.get(
            url,
            stream=True,
            timeout=REQUEST_TIMEOUT,
            headers={"Referer": "https://www.facebook.com/"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            return False

        size = 0
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    logger.warning("Image too large, skipping: %s", url)
                    break
                fh.write(chunk)

        if size < MIN_IMAGE_BYTES or size > MAX_IMAGE_BYTES:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        if os.path.exists(dest_path):
            os.remove(dest_path)
        logger.warning("Failed to download %s: %s", url, exc)
        return False


def run_download_job(profile_url: str, max_photos: int | None = None) -> tuple[str, int, str]:
    """
    Worker: fetches profile, crawls albums if requested amount needs more photos,
    downloads images, and packages a ZIP.

    Returns (zip_path, photo_count, slug).
    """
    target_count = min(max_photos or DEFAULT_MAX_PHOTOS, ABSOLUTE_MAX_PHOTOS)
    session = make_session()
    slug = profile_slug(profile_url)
    stripped = profile_url.rstrip("/")

    # If the user directly linked an album, only crawl that album
    if "set=a." in profile_url or "/media/set" in profile_url:
        candidates = [profile_url]
    elif stripped.endswith("/photos"):
        candidates = [stripped, stripped[:-7]]
    else:
        candidates = [stripped + "/photos", stripped]

    photo_urls: list[str] = []
    seen_ids: set[str] = set()
    hit_login_wall = False

    def add_from_page(html_data: str, page_link: str) -> None:
        for u in extract_photo_urls(html_data, page_link):
            fname = u.split("?")[0].split("/")[-1]
            if fname not in seen_ids:
                seen_ids.add(fname)
                photo_urls.append(u)
                if len(photo_urls) >= target_count:
                    break

    # 1. Fetch primary profile and /photos pages
    for page_url in candidates:
        try:
            resp = session.get(page_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", page_url, exc)
            continue

        html_content = resp.text
        if looks_like_login_wall(html_content, resp.url):
            hit_login_wall = True
            continue

        add_from_page(html_content, page_url)
        if len(photo_urls) >= target_count:
            break

    # 2. If user wants more photos than found on main pages, crawl /photos_albums
    if len(photo_urls) < target_count and "set=a." not in profile_url:
        albums_page_url = stripped + "/photos_albums"
        try:
            resp_alb = session.get(albums_page_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp_alb.status_code == 200:
                add_from_page(resp_alb.text, albums_page_url)
                album_links = find_album_links(resp_alb.text)
                logger.info("Discovered %d album(s) for %s", len(album_links), slug)

                for alb_url in album_links:
                    if len(photo_urls) >= target_count:
                        break
                    try:
                        r_single = session.get(alb_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                        if r_single.status_code == 200:
                            add_from_page(r_single.text, alb_url)
                    except Exception as exc:
                        logger.warning("Error fetching album %s: %s", alb_url, exc)
        except Exception as exc:
            logger.warning("Error checking photos_albums: %s", exc)

    if not photo_urls:
        if hit_login_wall:
            raise LoginRequiredError(
                "Facebook is asking for a login before showing this profile's photos.\n\n"
                "Tip: Check your FB_COOKIE in .env and verify you are logged in."
            )
        raise NoPhotosError(
            "I couldn't find any downloadable photos on that profile. "
            "Either the photos aren't accessible or Facebook blocked the request."
        )

    # 3. Download the collected photos
    workdir = tempfile.mkdtemp(prefix="fbphotos_")
    try:
        downloaded = 0
        for i, img_url in enumerate(photo_urls[:target_count], start=1):
            ext = img_url.split("?")[0].rsplit(".", 1)[-1].lower()
            if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
                ext = "jpg"
            photo_id = generate_unique_photo_id()
            dest = os.path.join(workdir, f"{photo_id}.{ext}")
            if download_image(session, img_url, dest):
                downloaded += 1

        if downloaded == 0:
            raise NoPhotosError(
                f"Found {len(photo_urls)} photo link(s) but downloads failed "
                "(Facebook may be rate-limiting). Please try again later."
            )

        zip_path = os.path.join(
            tempfile.gettempdir(), f"facebook-photos-{slug}.zip"
        )
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in sorted(os.listdir(workdir)):
                zf.write(os.path.join(workdir, fname), arcname=fname)

        return zip_path, downloaded, slug
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ----------------------------------------------------------------------------
# Message / Command Parsing
# ----------------------------------------------------------------------------
def parse_user_request(text: str) -> tuple[str | None, int | None]:
    """
    Parses messages such as:
      - /fb-pic https://facebook.com/profile 25
      - /fb_pic https://facebook.com/profile
      - https://facebook.com/profile 50
      - https://facebook.com/profile
    Returns (profile_url, pic_amount)
    """
    tokens = text.strip().split()
    url = None
    amount = None

    for token in tokens:
        if is_facebook_url(token):
            url = token
        elif token.isdigit():
            val = int(token)
            if val > 0:
                amount = min(val, ABSOLUTE_MAX_PHOTOS)

    return url, amount


# ----------------------------------------------------------------------------
# Telegram Bot Handlers
# ----------------------------------------------------------------------------
WELCOME = (
    "👋 *Welcome to Facebook Photo Downloader!*\n\n"
    "Send a Facebook profile link to download its photos as a ZIP file.\n\n"
    "**Commands:**\n"
    "• `/fb-pic <profile_url> <amount>`\n"
    "  Download up to a specific number of photos.\n"
    "  *Example:* `/fb-pic https://www.facebook.com/zuck 25`\n\n"
    "• `/fb-pic <profile_url>`\n"
    "  Download all available photos (up to 100).\n"
    "  *Example:* `/fb-pic https://www.facebook.com/zuck`\n\n"
    "• Or simply paste the link directly:\n"
    "  `https://www.facebook.com/zuck 30`"
)

HELP = (
    "📖 *How to use*\n\n"
    "1. **Specify an amount:**\n"
    "   `/fb-pic https://facebook.com/profile 20`\n"
    "   Or: `https://facebook.com/profile 20`\n\n"
    "2. **Download all available photos:**\n"
    "   `/fb-pic https://facebook.com/profile`\n"
    "   Or: `https://facebook.com/profile`\n\n"
    "The bot searches the profile, photos page, and albums to collect HD photos."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def handle_download_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    url, amount = parse_user_request(text)

    if not url:
        if text.startswith(("/", "fb-pic", "fb_pic")):
            await update.message.reply_text(
                "⚠️ Please provide a valid Facebook profile URL.\n\n"
                "Example:\n`/fb-pic https://www.facebook.com/username 25`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "Send me a Facebook profile link to download photos.\n"
                "Example:\n`/fb-pic https://www.facebook.com/username 20`",
                parse_mode="Markdown",
            )
        return

    amount_msg = f"up to {amount} photos" if amount else "all available photos"
    status = await update.message.reply_text(f"🔍 Searching for photos ({amount_msg})...")

    try:
        # Run blocking scraper and downloader in a background thread
        zip_path, count, slug = await asyncio.to_thread(run_download_job, url, amount)

        await status.edit_text(f"📥 Packaged {count} photo(s)! Sending ZIP file...")

        with open(zip_path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=f"facebook-photos-{slug}.zip",
                caption=f"✅ Done! {count} photo(s) from {url}",
            )
        if os.path.exists(zip_path):
            os.remove(zip_path)
        await status.delete()
    except BotError as exc:
        await status.edit_text(f"⚠️ {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error")
        await status.edit_text(
            f"❌ Something went wrong while processing ({exc}). "
            "Please try again later."
        )


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set in .env.\n"
            "Please add TELEGRAM_BOT_TOKEN to your .env file."
        )

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler(["fb_pic", "fbpic", "fb", "download"], handle_download_request))

    # Catch-all text handler (handles /fb-pic, bare URLs, URLs with amount)
    app.add_handler(
        MessageHandler(filters.TEXT, handle_download_request)
    )

    logger.info("Bot is running (polling)... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
