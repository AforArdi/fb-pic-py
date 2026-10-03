# Facebook Photo Downloader Telegram Bot (`fb-pic-py`)

A high-performance Python Telegram bot that extracts and downloads photos from Facebook profiles and albums into neatly packaged ZIP archives with full resolution preservation and globally unique naming.

---

## Features

- **High-Resolution Photo Extraction**: Analyzes Facebook's Relay/GraphQL payloads and CDN URLs to extract original, maximum-resolution images rather than compressed thumbnails.
- **Smart Avatar & Icon Filtering**: Automatically identifies and filters out commenter, follower, and friend avatar icons, ensuring only actual user photos are packaged.
- **Album & Multi-Page Deep Crawling**: Expands beyond the initial lazy-loaded grid by discovering and crawling profile photo subpages and user photo albums (`/photos_albums`, `/media/set/`).
- **Flexible Photo Quotas**: Specify exact quantities or download all available photos:
  - `/fb-pic <profile_url> <amount>`: Download a specified number of photos.
  - `/fb-pic <profile_url>`: Download all available photos (up to safety limit).
- **Globally Unique 8-Digit Filenames**: Every photo is saved with a collision-free 8-digit numeric filename (e.g. `67627782.png`, `82370145.jpg`). Used IDs are permanently tracked in a local registry (`used_photo_ids.txt`) to guarantee names are never repeated across different downloads or accounts.
- **Anti-Bot Firewall Bypass**: Emulates modern Chromium/Brave client hints (`Sec-Ch-Ua`, `Sec-Fetch-*`) and cookie sessions to prevent Facebook `400 Bad Request` and `403 Forbidden` CDN signature rejections.
- **Automated ZIP Packaging**: Downloads and sanitizes photos in isolated temporary directories and delivers them directly via Telegram document messages.

---

## Installation & Setup

### 1. Prerequisites
- Python 3.10 or higher
- Git

### 2. Clone the Repository
```bash
git clone https://github.com/AforArdi/fb-pic-py.git
cd fb-pic-py
```

### 3. Set Up Virtual Environment
```bash
# Windows (PowerShell)
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

---

## Configuration

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Open `.env` and fill in your credentials:
   ```env
   TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
   FB_COOKIE="c_user=...; xs=...; datr=...; sb=..."
   ```

### How to Get Your `FB_COOKIE`:
1. Log in to [facebook.com](https://www.facebook.com) in your browser (Chrome, Edge, or Brave).
2. Press `F12` (or right-click anywhere and click **Inspect**) to open Developer Tools.
3. Switch to the **Network** tab.
4. Refresh the page (`F5`).
5. Click on the first request (`www.facebook.com` or `facebook.com`).
6. In the **Headers** panel, scroll to **Request Headers**.
7. Copy the value of the `cookie:` header (starts with keys like `datr=...; sb=...; c_user=...; xs=...`).
8. Paste it into your `.env` file between the quotes for `FB_COOKIE`.

> **Note:** Never share your session cookies publicly or commit your `.env` file to version control.

---

## Usage

Start the bot:
```bash
# Windows
.\.venv\Scripts\python.exe fb_photo_bot.py

# Linux / macOS
python3 fb_photo_bot.py
```

### Bot Commands

| Command | Description | Example |
| :--- | :--- | :--- |
| `/start` | Display welcome information and quick start guide | `/start` |
| `/help` | Display syntax, usage instructions, and tips | `/help` |
| `/fb-pic <url> <amount>` | Download a specific number of photos from a profile | `/fb-pic https://www.facebook.com/username 25` |
| `/fb-pic <url>` | Download all available photos from a profile | `/fb-pic https://www.facebook.com/username` |
| `<url> [amount]` | Direct message shortcut without command prefix | `https://www.facebook.com/username 20` |

---

## Project Structure

```text
fb-pic-py/
├── .env.example          # Environment variables template
├── .gitignore            # Git exclusion rules (ignores secrets and venv)
├── fb_photo_bot.py       # Main Telegram bot application and scraper
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation
```

---

## Disclaimer

This project is created for educational, personal, and backup purposes only. Please respect intellectual property rights and only download photos you own or have explicit permission to access.
