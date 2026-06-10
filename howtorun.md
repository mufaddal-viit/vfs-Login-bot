# How to Run — VFS Login Bot

Step-by-step instructions to run the bot locally. Example uses **UAE → Luxembourg**
(`-sc AE -dc LU`); swap the codes for any supported pair (see the table at the bottom).

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
you're targeting** (e.g. the UAE→Luxembourg portal):

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

### Step 2 ("Your Details") fields — Luxembourg portal

The UAE → Luxembourg portal asks for more applicant details than the other portals.
Fill these in the `[applicant]` section of [config/config.ini](config/config.ini)
(any field left blank is skipped):

```ini
[applicant]
first_name = mufaddal
last_name = calcutta
nationality = india
passport_number = 9s9s923j0
gender = Male                 ; must match the dropdown text (Male / Female)
date_of_birth = 02/06/1995    ; DD/MM/YYYY
passport_expiry_date = 02/06/2030  ; DD/MM/YYYY
country_code = 971            ; dialling code, no '+'
contact_number = 501234567
email =                       ; blank -> reuses the [vfs-credential] login email
```

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
python -m src.main -sc AE -dc LU
```

**PowerShell:**

```powershell
cd d:\WORK\vfs-Login-bot
.\venv\Scripts\Activate.ps1
python -m src.main -sc AE -dc LU
```

> The installed console script also works the same way: `vfs-login-bot -sc AE -dc LU`

⚠️ Country codes are **case-sensitive** — use uppercase (`-dc LU`, not `-dc lu`).

---

## Step 4 — Check the result

- Live logs print in Terminal 2 and append to `app.log`.
- Step-by-step screenshots land in [screenshots/](screenshots/), numbered in flow order:
  `06_start_new_booking` → `07_appointment_details` → `08_after_continue` →
  `09_your_details_filled` → `10_after_save` → `13_otp_sent` → `14_otp_verified` →
  `15_book_appointment` → `16_date_selected` → `16b_time_selected` →
  `17_step3_complete` → `18_services` → `19_review_accepted` → `20_pay_online`.
  Any step that fails also drops an `ERROR_*` screenshot.

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
| `-sc AE -dc LU`    | UAE          | Luxembourg  | `visa.vfsglobal.com/are/en/lux/login` |

To add a new pair: add the URL to [config/vfs_urls.ini](config/vfs_urls.ini) keyed
`SRC-DST` (e.g. `AE-LU`). If the **destination** is already supported (DE, IT, MT, LU),
that's all you need — any source country works. For a brand-new destination, also create a
`VfsBot<XX>` class in `src/vfs_bot/` and register it in
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
