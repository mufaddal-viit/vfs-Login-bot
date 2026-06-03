# How to Run — VFS Login Bot

Step-by-step instructions to run the bot locally. Example uses **India → Luxembourg**
(`-sc IN -dc LU`); swap the codes for any supported pair (see the table at the bottom).

The bot attaches to a **real Chrome** you launch yourself (this is how it gets past
Cloudflare). So you always need **two terminals**: one running Chrome, one running the bot.

---

## One-time setup

Only needed the first time (already done on this machine).

```bash
# from the project root: /d/WORK/vfs-Login-bot
python -m venv venv
source venv/Scripts/activate              # Git Bash
pip install playwright "playwright-stealth==1.0.6" "setuptools<81"
pip install -e .
```

---

## Step 1 — Put your credentials in the config

Edit [config/config.ini](config/config.ini) with a VFS account **registered on the portal
you're targeting** (e.g. the India→Luxembourg portal):

```ini
[browser]
type = chromium
headless = false
cdp_url = http://localhost:9222

[vfs-credential]
email = your.registered.email@example.com
password = YourRealPassword
```

> If the email isn't registered on that specific portal, login will fail with
> *"The entered email id is not registered with us."*

---

## Step 2 — Terminal 1: launch the debugging Chrome

Leave this window open while the bot runs. It uses a throwaway profile, so your normal
Chrome can stay open.

**Git Bash:**

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$TEMP/vfs-chrome-profile" \
  --window-size=1280,720
```

**PowerShell:**

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\vfs-chrome-profile" --window-size=1280,720
```

Verify it's up (optional) — should print JSON:

```bash
curl -s http://localhost:9222/json/version
```

---

## Step 3 — Terminal 2: run the bot

**Git Bash** (if your prompt already shows `(venv)`, skip the `source` line):

```bash
cd /d/WORK/vfs-Login-bot
source venv/Scripts/activate
python -m src.main -sc IN -dc LU
```

**PowerShell:**

```powershell
cd d:\WORK\vfs-Login-bot
.\venv\Scripts\Activate.ps1
python -m src.main -sc IN -dc LU
```

> The installed console script also works the same way: `vfs-login-bot -sc IN -dc LU`

⚠️ Country codes are **case-sensitive** — use uppercase (`-dc LU`, not `-dc lu`).

---

## Step 4 — Check the result

- Live logs print in Terminal 2 and append to `app.log`.
- Step-by-step screenshots land in [screenshots/](screenshots/):
  `01_pre_login_done` → `02_before_sign_in` → `03_after_sign_in` →
  `04_dashboard` → `05_after_sign_out`.

---

## Step 5 — Stop the debugging Chrome

Close the Terminal-1 Chrome window, or kill only the debug instance:

**Git Bash:**

```bash
curl -s http://localhost:9222/json/version >/dev/null && echo "still up" || echo "closed"
# to force-stop the debug instance:
taskkill //F //FI "WINDOWTITLE eq *9222*" 2>/dev/null
```

**PowerShell (targets only the debug instance, leaves normal Chrome alone):**

```powershell
Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
  Where-Object { $_.CommandLine -like '*remote-debugging-port=9222*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

---

## Supported country pairs

| Command            | Source       | Destination | Portal URL |
| ------------------ | ------------ | ----------- | ---------- |
| `-sc IN -dc DE`    | India        | Germany     | `visa.vfsglobal.com/ind/en/deu/login` |
| `-sc IQ -dc DE`    | Iraq         | Germany     | `visa.vfsglobal.com/irq/en/deu/login` |
| `-sc MA -dc IT`    | Morocco      | Italy       | `visa.vfsglobal.com/mar/en/ita/login` |
| `-sc AZ -dc IT`    | Azerbaijan   | Italy       | `visa.vfsglobal.com/aze/en/ita/login` |
| `-sc AE -dc MT`    | UAE          | Malta       | `visa.vfsglobal.com/are/en/mlt/login` |
| `-sc IN -dc LU`    | India        | Luxembourg  | `visa.vfsglobal.com/ind/en/lux/login` |

To add a new pair: add the URL to [config/vfs_urls.ini](config/vfs_urls.ini), create a
`VfsBot<XX>` class in `src/vfs_bot/`, and register it in
[src/vfs_bot/vfs_bot_factory.py](src/vfs_bot/vfs_bot_factory.py).

---

## Troubleshooting

| Symptom | Cause / Fix |
| ------- | ----------- |
| `bash: .venvScriptspython.exe: command not found` | You used PowerShell backslashes in Git Bash. Use forward slashes, or just `python ...` since the venv is active. |
| `Country LU is not supported` | Used lowercase. Codes are case-sensitive — use uppercase. |
| `The entered email id is not registered` | The account in `config.ini` isn't registered on that portal. Use a registered account. |
| `Did not reach /dashboard` | Login didn't complete (wrong creds, or portal changed). Check `03_after_sign_in.png`. |
| Bot can't connect to Chrome | Terminal-1 Chrome isn't running, or port 9222 is taken. Re-run Step 2 and verify with the `curl` check. |
