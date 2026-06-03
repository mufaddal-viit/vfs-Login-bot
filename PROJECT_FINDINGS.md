# VFS Login Bot — Project Findings & Code Walkthrough

> A complete, single-file analysis of the `vfs-Login-bot` project: what it does, how the
> code is structured, the end-to-end execution flow, every file's purpose, and notable
> observations / issues found while reading each file.

---

## 1. What This Project Does (Summary)

**VFS Login Bot** is a Python + Playwright automation tool that logs in to the
**VFS Global** visa-appointment portal (`visa.vfsglobal.com`).

The portal is an Angular single-page app sitting behind **Cloudflare** enterprise bot
protection (Turnstile challenge + WAF + JA3 TLS fingerprinting + behavioral analysis). A
normal Playwright-launched browser gets blocked, so the bot's core trick is:

- The user **manually launches a real Chrome** with `--remote-debugging-port=9222`.
- Cloudflare sees a genuine browser fingerprint and lets the page load (passes Turnstile).
- The bot **attaches to that real Chrome via CDP** (Chrome DevTools Protocol) and drives the
  UI — filling email/password, clicking **Sign In**, waiting for the dashboard, then signing
  out.
- Screenshots are captured at every stage into `screenshots/` for debugging.

It is **login-only and on-demand** — no polling, no appointment scraping — deliberately to
stay under VFS rate limits. The `run()` method always returns `False` (login-only flow, no
booking).

**Scope:** personal use. Supports 5 source→destination country pairs (see §6).

---

## 2. Tech Stack

| Concern            | Choice                                                        |
| ------------------ | ------------------------------------------------------------- |
| Language           | Python `^3.9` (tested on 3.12)                                |
| Browser automation | `playwright ^1.43.0` (sync API)                               |
| Anti-detection     | `playwright-stealth ^1.0.6` (`stealth_sync`)                  |
| Config             | `configparser` reading `.ini` files                           |
| Packaging          | Poetry (`pyproject.toml`), console script `vfs-login-bot`     |
| Lint / format      | `flake8 ^7`, `black ^24` (dev deps)                           |
| CI/CD              | GitHub Actions (build, CodeQL, dependency-review, PyPI publish, OpenSSF scorecard) |

---

## 3. File-by-File Inventory

### Source code (`src/`)

| File | Role |
| ---- | ---- |
| [src/main.py](src/main.py) | Entry point. Sets up logging, parses `-sc`/`-dc` CLI args, builds the right bot via the factory, runs it, and handles top-level exceptions. |
| [src/utils/config_reader.py](src/utils/config_reader.py) | Loads **all** `*.ini` files from `config/` into a single cached `ConfigParser`. Also merges a user file pointed to by `VFS_BOT_CONFIG_PATH`. Exposes `get_config_section` / `get_config_value`. |
| [src/vfs_bot/vfs_bot.py](src/vfs_bot/vfs_bot.py) | **Abstract base class `VfsBot`** (the template method). Defines `run()` (the full browser-orchestration skeleton), screenshot + sign-out helpers, and abstract `login()` / `pre_login_steps()`. Also defines `LoginError`. |
| [src/vfs_bot/vfs_bot_factory.py](src/vfs_bot/vfs_bot_factory.py) | `get_vfs_bot(sc, dc)` dispatcher: maps **destination** country code → concrete bot class (`DE`/`IT`/`MT`). Raises `UnsupportedCountryError` otherwise. |
| [src/vfs_bot/vfs_bot_de.py](src/vfs_bot/vfs_bot_de.py) | Germany (`DE`) implementation of `login()` + `pre_login_steps()`. |
| [src/vfs_bot/vfs_bot_it.py](src/vfs_bot/vfs_bot_it.py) | Italy (`IT`) implementation — **byte-for-byte identical logic** to the DE version. |
| [src/vfs_bot/vfs_bot_mt.py](src/vfs_bot/vfs_bot_mt.py) | Malta (`MT`) implementation — same logic, slightly different docstrings. |

### Configuration (`config/`)

| File | Role |
| ---- | ---- |
| [config/config.ini](config/config.ini) | Browser settings (`type`, `headless`, `cdp_url`) + **VFS credentials** (`email`, `password`). |
| [config/vfs_urls.ini](config/vfs_urls.ini) | Maps `SRC-DST` keys (e.g. `IN-DE`) → the VFS login URL. |

### Tooling / meta

| File | Role |
| ---- | ---- |
| [pyproject.toml](pyproject.toml) | Poetry project metadata, deps, and the `vfs-login-bot = "src.main:main"` console-script entry point. |
| [.flake8](.flake8) | Lint config — max line length 120, max complexity 10, excludes `config/`, `venv/`, etc. |
| [README.md](README.md) | Detailed docs: how it works, Cloudflare challenges overcome, setup, usage, country table. |
| `.github/workflows/*` | CI: `build.yml`, `codeql.yml`, `dependency-review.yml`, `publish.yml`, `publish-testpypi.yml`, `scorecard.yml`. Uses pinned action SHAs + step-security harden-runner (good supply-chain hygiene). |
| `screenshots/*.png` | ~40 committed debug screenshots from past runs (timestamps in April 2026). |
| `src/**/__pycache__/*.pyc` | Compiled bytecode that has been **committed to git** (should be ignored). |

---

## 4. Execution Flow (End-to-End)

### 4.1 Startup & dispatch

```
$ vfs-login-bot -sc IN -dc DE
        │
        ▼
main.main()                                    [src/main.py]
  ├─ initialize_logger()        → logs to console + app.log
  ├─ initialize_config()        → loads every config/*.ini (+ $VFS_BOT_CONFIG_PATH)
  ├─ argparse                   → source_country_code="IN", destination_country_code="DE"
  ├─ get_vfs_bot("IN", "DE")    → returns VfsBotDe("IN")        [vfs_bot_factory.py]
  └─ vfs_bot.run()              → runs the shared flow          [vfs_bot.py]
```

`get_vfs_bot` keys **only on the destination** code (`DE`/`IT`/`MT`); the source code is
just passed into the bot's constructor and used to build the URL key.

### 4.2 The `run()` template method  ([vfs_bot.py](src/vfs_bot/vfs_bot.py))

```
run()
 ├─ read browser.type / browser.headless
 ├─ build url_key = "IN-DE", look up vfs-url → the login URL
 ├─ read vfs-credential.email / .password
 ├─ makedirs("screenshots")
 └─ with sync_playwright():
      ├─ IF browser.cdp_url set  → connect_over_cdp(cdp_url)        ← THE PRIMARY PATH
      │     reuse existing context, open new tab
      │
      └─ ELSE → launch own browser (chromium gets
      │         --disable-blink-features=AutomationControlled,
      │         --no-sandbox; firefox/webkit plain), new context
      │         (1280×720), then stealth_sync(page)
      │
      ├─ page.goto(vfs_url, 60s, wait_until="domcontentloaded")
      ├─ pre_login_steps(page)            ← abstract → subclass (reject cookies)
      ├─ screenshot "01_pre_login_done"
      ├─ try: login(page, email, password)   ← abstract → subclass
      │   except: screenshot "ERROR_login_failed", log error
      └─ finally: page.close(); browser.close()
      return False
```

This is a classic **Template Method** pattern: `run()` fixes the skeleton; subclasses fill
in the two country-specific hooks.

### 4.3 Per-country `login()` (DE / IT / MT — effectively identical)

```
login(page, email, password)
 ├─ wait_for_selector( username field )         timeout 120s  ← waits out Cloudflare
 ├─ click email  → press_sequentially(email, delay=200ms)     ← human-like typing
 ├─ click pwd    → press_sequentially(password, delay=200ms)
 ├─ screenshot "02_before_sign_in"
 ├─ click button "Sign In"
 ├─ screenshot "03_after_sign_in"
 └─ try:
      ├─ wait_for_url("**/dashboard", 60s)
      ├─ screenshot "04_dashboard"
      └─ _sign_out(page)  → click "Sign Out", screenshot "05_after_sign_out"
    except: log "Did not reach /dashboard"
```

The selectors are resilient (3 fallbacks each):
`input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']` and the
password equivalent — covering different VFS Angular Material markup variants.

### 4.4 `pre_login_steps()` (all three countries identical)

Attempts to click a **"Reject All"** cookie button (5s timeout); silently skips if absent.

---

## 5. Configuration Model

`initialize_config()` reads **every** `.ini` in `config/` into one shared `ConfigParser`,
so sections from `config.ini` and `vfs_urls.ini` are merged into a single namespace:

- `[browser]` → `type`, `headless`, `cdp_url`
- `[vfs-credential]` → `email`, `password`
- `[vfs-url]` → one key per `SRC-DST` pair

An optional external file via `VFS_BOT_CONFIG_PATH` is layered on top (lets users keep
secrets outside the repo). Lookups go through `get_config_value(section, key, default)`.

Key behavior: **`cdp_url` being set makes `headless` irrelevant** — the CDP branch is taken
and the bot attaches to whatever Chrome the user already launched. With the committed
`config.ini` (`cdp_url = http://localhost:9222`), the CDP path is always used.

---

## 6. Supported Country Pairs  ([config/vfs_urls.ini](config/vfs_urls.ini))

| Key (`SRC-DST`) | Source       | Destination | URL |
| --------------- | ------------ | ----------- | --- |
| `IN-DE`         | India        | Germany     | `visa.vfsglobal.com/ind/en/deu/login` |
| `IQ-DE`         | Iraq         | Germany     | `visa.vfsglobal.com/irq/en/deu/login` |
| `MA-IT`         | Morocco      | Italy       | `visa.vfsglobal.com/mar/en/ita/login` |
| `AZ-IT`         | Azerbaijan   | Italy       | `visa.vfsglobal.com/aze/en/ita/login` |
| `AE-MT`         | UAE          | Malta       | `visa.vfsglobal.com/are/en/mlt/login` |

Dispatch is by **destination only** — so any source country can pair with a supported
destination as long as the `SRC-DST` URL key exists in `vfs_urls.ini`.

---

## 7. How to Run (condensed)

1. **Launch a real Chrome** with remote debugging (and a throwaway profile):
   ```
   "C:\Program Files\Google\Chrome\Application\chrome.exe" \
     --remote-debugging-port=9222 --user-data-dir="%TEMP%\vfs-chrome-profile"
   ```
2. (Optional) `export VFS_BOT_CONFIG_PATH=...\config\config.ini` if running outside the repo.
3. `vfs-login-bot -sc AE -dc MT`

Outputs: step-by-step PNGs in `screenshots/`, logs to console and `app.log`.

---

## 8. Observations, Risks & Issues Found

### 🔴 Security
1. **Real credentials are committed in plaintext** in [config/config.ini](config/config.ini)
   (`calcutta53.mufaddal@gmail.com` / a real-looking password), and the same email is the
   author email in `pyproject.toml`. **This account is effectively compromised — the
   password should be rotated immediately and removed from git history.** Going forward,
   credentials belong only in an out-of-repo file referenced by `VFS_BOT_CONFIG_PATH`, and
   `config.ini` should be git-ignored (ship a `config.ini.example` instead).

### 🟠 Code duplication / maintainability
2. **`vfs_bot_de.py`, `vfs_bot_it.py`, and `vfs_bot_mt.py` are essentially identical.** The
   entire `login()` body and `pre_login_steps()` are copy-pasted. This should collapse into
   a single shared implementation in the base class (or one concrete class parameterized by
   `destination_country_code`), leaving subclasses to override only genuine differences.
3. **Factory uses string equality on raw input** (`destination_country_code == "DE"`), so it
   is **case-sensitive** — `-dc de` would fail with `UnsupportedCountryError`. Normalize to
   upper-case. (Note the misleading local var `country_lower` that is never lower-cased.)

### 🟡 Correctness / robustness
4. **`run()` swallows config errors quietly.** `get_config_value` returns `None` on a missing
   key rather than raising `KeyError`, so the `except KeyError` guard around the URL lookup
   never fires; an unknown pair yields `vfs_url = None` and a later failure instead of a clear
   message.
5. **Non-CDP branch never gets stealth on the CDP branch** (by design), but the CDP branch
   opens `context.new_page()` on an existing context — if the user's launched Chrome has no
   contexts, `browser.new_context()` is created but the page is still opened correctly; fine,
   just worth noting the dependency on the user's Chrome state.
6. **`LoginError` is defined but never raised.** `main.py` catches it, but no code path
   produces it; login failures are caught inside `run()` and only logged.
7. Heavy reliance on **fixed `wait_for_timeout` sleeps** (800–4000 ms) makes runs slow and
   flaky vs. event-based waits.

### 🟢 Docs / repo hygiene
8. **README drift:** the flow diagram references a `01_page_loaded.png` screenshot that the
   code never produces (code emits `01_pre_login_done`), and the "Project Structure" section
   omits `vfs_bot_mt.py` and the Malta/UAE support that actually exists in code.
9. **Committed build artifacts:** `__pycache__/*.pyc` files are tracked in git and should be
   added to `.gitignore`. ~40 historical screenshots are also committed.
10. **`pyproject.toml` declares Playwright as a dependency** but the README install steps
    install it manually with a pinned `setuptools<81` — the documented install path diverges
    from the declared package metadata.

### ⚖️ Legal / ToS
11. The tool automates a portal protected by Cloudflare and is explicitly designed to bypass
    bot protection. The README disclaimer notes personal-use-only and ToS responsibility; this
    is genuinely **dual-use** and may violate VFS Global's Terms of Service. Users bear the
    risk of account bans / IP blocks.

---

## 9. Architecture at a Glance

```
                         CLI: vfs-login-bot -sc IN -dc DE
                                      │
                                      ▼
                              ┌──────────────┐
                              │   main.py    │  logging, argparse, top-level errors
                              └──────┬───────┘
                                     │
                  ┌──────────────────┼───────────────────┐
                  ▼                  ▼                    ▼
          config_reader.py   vfs_bot_factory.py    (loads config/*.ini)
          (ini → dict)        dc → bot class
                                     │
                  ┌──────────────────┼──────────────────┐
                  ▼                  ▼                   ▼
            VfsBotDe            VfsBotIt            VfsBotMt
                  └──────────────────┼──────────────────┘
                                     ▼
                          VfsBot (ABC)  ── run() template method
                                     │  (Playwright + CDP + screenshots)
                                     ▼
                       Real Chrome (CDP :9222) → VFS Global portal
```

**Design patterns:** Template Method (`VfsBot.run`), Factory (`get_vfs_bot`), and a small
singleton-ish cached config module.

---

*Generated from a full read of every source, config, and tooling file in the repository.*
