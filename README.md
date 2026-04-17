# VFS Login Bot

An automated login bot for the **VFS Global** visa appointment portal. Built with **Python** and **Playwright**, this tool opens your Chrome browser, navigates to the VFS login page, bypasses the Cloudflare Turnstile challenge, and signs you in with your credentials — capturing screenshots at every step for debugging.

## Features

- **Cloudflare Turnstile bypass** using Chrome's remote debugging protocol (CDP)
- **Multi-country support** — India, Iraq, Morocco, Azerbaijan (for German/Italian visas)
<!-- - **Automatic screenshots** at every stage (page load, pre-login, after sign-in, errors) -->
- **Configurable** via INI files — no hardcoded credentials
- **Clean exit** — browser closes cleanly after login flow completes
- **Detailed logging** to both console and `app.log`

---

## How It Works

The bot cannot use a standard Playwright-launched browser because VFS Global sits behind Cloudflare, which blocks headless/automated browsers from loading the Angular app. Instead, the bot:

1. Connects to a **real Chrome instance** you launch yourself with `--remote-debugging-port=9222`
2. Cloudflare sees a genuine Chrome browser and passes the Turnstile check
3. Playwright controls this real browser via the CDP protocol
4. The bot fills in credentials, clicks Sign In, and takes a final screenshot

This is the key trick — **your browser does the heavy lifting**, the bot just automates it.

### Flow Diagram

```
You launch Chrome with --remote-debugging-port=9222
                 │
                 ▼
     Bot connects via CDP to port 9222
                 │
                 ▼
    Bot opens new tab → navigates to VFS URL
                 │
                 ▼
  Cloudflare Turnstile passes (real browser)
                 │
                 ▼
   Dismiss cookie banner (if present)
                 │
                 ▼
    Screenshot: 01_pre_login_done.png
                 │
                 ▼
       Fill email + password
                 │
                 ▼
    Screenshot: 02_before_sign_in.png
                 │
                 ▼
           Click Sign In
                 │
                 ▼
    Screenshot: 03_after_sign_in.png
                 │
                 ▼
     Wait for URL → **/dashboard
                 │
                 ▼
    Screenshot: 04_dashboard.png
                 │
                 ▼
          Click Sign Out
                 │
                 ▼
    Screenshot: 05_after_sign_out.png
                 │
                 ▼
         Browser closes cleanly
```

---

## Challenges Overcome

VFS Global is one of the hardest portals to automate. Here's what this project had to work around:

### 1. Cloudflare Bot Management

VFS sits behind Cloudflare's enterprise bot protection, which blocks roughly 15% of traffic as automated. It detects bots via:

- **JA3 TLS fingerprinting** — identifies automation libraries by their TLS handshake signature
- **Behavioral analysis** — scoring mouse movement, scroll patterns, and timing
- **Traffic pattern detection** — flagging anything that doesn't look like a real user session

**How we bypass it:** the bot never launches its own browser. Instead, it connects via CDP (Chrome DevTools Protocol) to a real Chrome instance the user starts manually. Cloudflare sees a genuine browser fingerprint and lets it through.

### 2. Cloudflare Turnstile Challenge

The login flow includes a `cf-turnstile-response` token, confirmed via request payloads in dev tools. A Playwright-launched browser fails the Turnstile check and the page never finishes loading.

**How we bypass it:** since we're driving a real Chrome instance (not a Playwright-spawned one), Turnstile passes naturally — no token-solving service required.

### 3. Rate Limiting

VFS enforces strict limits on:

- Login attempts
- Calendar / appointment checks
- Registration attempts

Hit the limit and your account or IP gets temporarily blocked.

**How we handle it:** this bot is login-only and runs on demand — no polling loops, no appointment scraping. One invocation = one login attempt. Stay well under any threshold.

### 4. Cloudflare WAF (Web Application Firewall)

The WAF layer blocks:

- OWASP Top 10 attack patterns
- Direct API hits (e.g. requests straight to `/login`)
- Suspicious payloads and unusual request structures

**How we handle it:** the bot drives the actual Angular UI — filling inputs, clicking buttons — so every request originates from the real browser with legitimate headers, cookies, and session state. No direct API calls.

---

## Supported Countries

| Source Country  | Destination  | URL                                   |
| --------------- | ------------ | ------------------------------------- |
| India (IN)      | Germany (DE) | `visa.vfsglobal.com/ind/en/deu/login` |
| Iraq (IQ)       | Germany (DE) | `visa.vfsglobal.com/irq/en/deu/login` |
| Morocco (MA)    | Italy (IT)   | `visa.vfsglobal.com/mar/en/ita/login` |
| Azerbaijan (AZ) | Italy (IT)   | `visa.vfsglobal.com/aze/en/ita/login` |
| UAE (AE)        | Malta (MT)   | `visa.vfsglobal.com/are/en/mlt/login` |

Country codes follow [ISO 3166-1 alpha-2](https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2).

Add more URLs to [config/vfs_urls.ini](config/vfs_urls.ini) to extend support.

---

## Prerequisites

- **Python 3.9 – 3.12** (tested on 3.12)
- **Google Chrome** installed at the default location
- Windows / macOS / Linux (paths differ on each — examples below are for Windows)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/mufaddal-viit/vfs-Login-bot.git
cd vfs-login-bot
```

### 2. Create a virtual environment

```bash
python -m venv venv
```

### 3. Activate the venv

**Windows (Git Bash):**

```bash
source venv/Scripts/activate
```

**Windows (CMD):**

```cmd
venv\Scripts\activate
```

### 4. Install dependencies

```bash
pip install playwright "playwright-stealth==1.0.6" "setuptools<81"
pip install -e .
```

### 5. Install Playwright browser drivers

```bash
playwright install chromium
```

---

## Configuration

Edit [config/config.ini](config/config.ini) with your VFS credentials:

```ini
[browser]
type = chromium
headless = false
cdp_url = http://localhost:9222

[vfs-credential]
email = your.email@gmail.com
password = YourPassword123
```

### Config reference

| Section          | Key        | Description                                      |
| ---------------- | ---------- | ------------------------------------------------ |
| `browser`        | `type`     | Browser engine (`chromium`, `firefox`, `webkit`) |
| `browser`        | `headless` | `true` / `false` — ignored when `cdp_url` is set |
| `browser`        | `cdp_url`  | Remote debugging URL for your Chrome instance    |
| `vfs-credential` | `email`    | Your registered VFS Global account email         |
| `vfs-credential` | `password` | Your VFS Global account password                 |

---

## Usage

### Step 1 — Launch Chrome with remote debugging

Close all existing Chrome windows first, then open a terminal:

**Windows (Git Bash):**

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$TEMP/vfs-chrome-profile"
```

> The `--user-data-dir` flag uses a throwaway profile so this Chrome instance doesn't interfere with your regular browser.

### Step 2 — Export the config path (if outside the repo)

```bash
export VFS_BOT_CONFIG_PATH="/absolute/path/to/config/config.ini"
```

### Step 3 — Run the bot

```bash
vfs-login-bot -sc IN -dc DE
```

### Step 4 — All in one command after chrome debug opened

```bash
cd "/c/Users/DELL/Documents/GitHub/New folder/vfs-login-bot" && source venv/Scripts/activate && export VFS_BOT_CONFIG_PATH="c:/Users/DELL/Documents/GitHub/New folder/vfs-login-bot/config/config.ini" && vfs-login-bot -sc IN -dc DE

```

**Command-line arguments:**

| Flag  | Long form                    | Description                            | Required |
| ----- | ---------------------------- | -------------------------------------- | -------- |
| `-sc` | `--source-country-code`      | ISO 3166-1 alpha-2 source country      | Yes      |
| `-dc` | `--destination-country-code` | ISO 3166-1 alpha-2 destination country | Yes      |

---

## Project Structure

```
vfs-login-bot/
├── config/
│   ├── config.ini          # User credentials & browser settings
│   └── vfs_urls.ini        # VFS Global URLs per country pair
├── src/
│   ├── main.py             # Entry point & CLI argument parsing
│   ├── utils/
│   │   └── config_reader.py
│   └── vfs_bot/
│       ├── vfs_bot.py         # Abstract base class (shared logic)
│       ├── vfs_bot_de.py      # Germany implementation
│       ├── vfs_bot_it.py      # Italy implementation
│       └── vfs_bot_factory.py # Country → bot class dispatcher
├── screenshots/            # Auto-generated debug screenshots
├── app.log                 # Runtime logs
├── pyproject.toml
└── README.md
```

---

## How to Add a New Country

1. Add the login URL to [config/vfs_urls.ini](config/vfs_urls.ini):

   ```ini
   XX-YY = https://visa.vfsglobal.com/xxx/en/yyy/login
   ```

2. Create a new bot class `src/vfs_bot/vfs_bot_yy.py` extending `VfsBot` and implement:
   - `login()`
   - `pre_login_steps()`

3. Register it in [src/vfs_bot/vfs_bot_factory.py](src/vfs_bot/vfs_bot_factory.py)

---

## Disclaimer

This tool is for **personal use only**. It automates the same actions a user would perform manually in a browser. You are responsible for complying with VFS Global's [Terms of Service](https://www.vfsglobal.com/). The author accepts no liability for account bans, rate-limiting, or any consequences of using this tool.
