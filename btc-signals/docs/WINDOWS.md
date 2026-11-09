# Running the bot on Windows 10 / 11

All commands are for **PowerShell** (Start menu → type "PowerShell" → Windows PowerShell).
Nothing here changes trading logic or safety settings: the bot is in **PAPER** mode until you
change that yourself after the live-readiness steps.

---

## 1. Install Python (once)

1. Download **Python 3.12** (64-bit) from https://www.python.org/downloads/windows/
   (3.9 or newer works; 3.12 is recommended).
2. In the installer, tick **"Add python.exe to PATH"**, then click **Install Now**.
3. Close PowerShell and open a **new** window, then check:
   ```powershell
   py -3 --version
   ```
   It should print `Python 3.12.x`. If Windows opens the Microsoft Store instead, turn off the
   Store shortcut: Settings → Apps → Advanced app settings → **App execution aliases** → switch off
   `python.exe` and `python3.exe`.

Git (optional, to download the code): https://git-scm.com/download/win, then
```powershell
cd $HOME\Documents
git clone https://github.com/tirzec/ubtw.git
cd ubtw\btc-signals
```
(If you downloaded a ZIP from GitHub instead, unzip it, `cd` into `btc-signals`, and run
`Get-ChildItem -Recurse | Unblock-File` once so Windows lets the scripts run.)

## 2. Set up (once): virtual environment, dependencies, .env

Easiest: double-click **`windows\setup.bat`**, or run it from PowerShell:
```powershell
.\windows\setup.bat
```
It creates `.venv`, installs the requirements, copies `.env.example` to `.env`, runs the tests and
checks your clock.

The same steps by hand:
```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m pytest -q
python -m app.clock
```
Once activated, the prompt starts with `(.venv)`. Each new PowerShell window needs
`.\.venv\Scripts\Activate.ps1` again (the `windows\*.bat` scripts do it for you).

### .env
```powershell
notepad .env
```
Fill in only what you use (phone alerts, odds keys; Polymarket US keys come later in the
live-readiness steps). Save. The file is in `.gitignore`, never logged, and never committed.
Notepad sometimes saves with a hidden "BOM" marker at the start; the app handles that.

## 3. Start the bot and dashboard

| | |
|---|---|
| `.\windows\start-bot.bat` | **background**: keeps running after you close PowerShell. Opens http://localhost:8000. Output goes to `data\console.log`. |
| `.\windows\start-bot.ps1 -Visible` | in its own window (close that window = bot stops). **Required for LIVE mode**: the live check makes you type a confirmation phrase, so it refuses to start hidden. |
| `python -m app.main` | in the current window (`.venv` activated). Ctrl+C stops it. |

Add `-Https` (or `--https` for `python -m app.main`) if your browser forces https for localhost.
The certificate is made by the app itself (no openssl needed). The browser warns once:
**Advanced → Continue to localhost**.

If Windows Firewall asks about Python: you can click **Cancel**. The dashboard only listens on
`127.0.0.1` (this PC), so it needs no firewall opening.

Watch the log live: `Get-Content data\console.log -Wait -Tail 30`

## 4. Alerts

- **Desktop**: open the dashboard in Chrome or Edge, click **Enable alerts**, allow notifications.
  Alerts show as Windows notifications with a sound while the dashboard tab is open (it can be in
  the background). If none appear: Settings → System → Notifications → make sure Chrome/Edge is
  allowed, and **Do not disturb / Focus** is off.
- **Phone** (works when the PC dashboard is closed, as long as the bot runs): ntfy or Telegram,
  configured in `.env` exactly as in the README "Phone alerts" section. Test it:
  ```powershell
  .\.venv\Scripts\python.exe -m app.notify
  ```

## 5. Stop safely

```powershell
.\windows\stop-bot.bat
```
1. Sets the **kill switch**: the bot cancels every open order and halts. The script waits until
   the bot confirms (`confirmed: crypto_updown halted, N open orders canceled`).
2. Then ends the bot process.

Open positions are **not** sold; check them in the Polymarket app. The stop **stays set**: after
the next start, the bot stays halted until you clear it:
```powershell
.\windows\resume.bat        # or press Resume on the dashboard
.\windows\status.bat        # mode, risk state, open positions
```
For an emergency stop while it keeps running (dashboard stays up): the **STOP** button on the
dashboard, or `.\.venv\Scripts\python.exe -m app.trader stop`.

Don't end the bot with Task Manager (or by shutting down) while it has orders out without using the
kill switch first. On the next start it cancels any leftover open orders anyway, but until then they
can still fill.

## 6. The PC clock (the 5-minute markets depend on it)

```powershell
.\.venv\Scripts\python.exe -m app.clock
```
- `[PASS]` = within 1 s. `[WARN]` = 1–2 s off. `[FAIL]` = more than 2 s off, or no time server reachable.
- It asks an NTP server (time.windows.com); if your network blocks that, it uses Polymarket's own
  server clock instead. It also shows whether the Windows Time service is running and when it last synced.
- The dashboard start-up message shows the same check.

**Turn on automatic time sync:**
1. Settings → **Time & language** → **Date & time** → turn on **Set time automatically** and
   **Set time zone automatically** → click **Sync now**.
2. If Sync now fails, open PowerShell **as Administrator** (right-click → Run as administrator):
   ```powershell
   Set-Service w32time -StartupType Automatic
   Start-Service w32time
   w32tm /resync
   w32tm /query /status
   ```
   `Last Successful Sync Time` should be recent and `Source` should be `time.windows.com` (or similar).

## 7. Keep it running: sleep, power and updates

The bot already stops Windows from **idle-sleeping** while it runs (`Keep awake: on` in the
start-up message; turn off with `server.keep_awake: false`). It can't stop these, so change them:

**Power** (Settings → System → **Power & battery** (Win 11) / Power & sleep (Win 10)):
- *Screen and sleep* → **When plugged in, put my device to sleep after: Never** (the screen
  turning off is fine).
- Power mode: **Best performance** (or Balanced). Keep a laptop **plugged in**.
- Laptop lid: Control Panel → Hardware and Sound → Power Options → **Choose what closing the lid does**
  → *When plugged in*: **Do nothing**.
- Same window, **Change settings that are currently unavailable** → untick **Turn on fast startup**
  (avoids half-hibernated restarts).
- Settings → Network & internet → Wi-Fi/Ethernet → your network → **Metered connection: Off** and,
  in Device Manager → Network adapters → your adapter → Power Management, untick
  **Allow the computer to turn off this device to save power**. Wired Ethernet is more reliable than Wi-Fi.

**Windows Update** (Settings → Windows Update):
- **Advanced options → Active hours**: set them to cover when the bot trades (up to 18 hours), so
  Windows doesn't restart then.
- Turn off **"Get me up to date"** (Win 11) so it doesn't force a restart as soon as possible.
- Before a long run, you can **Pause updates** for 1–5 weeks.
- After any restart, start the bot again (`windows\start-bot.bat`). It loads open positions, cancels
  stray orders and stays halted if the kill switch was set.

**Polymarket maintenance**: every Thursday 6–8 AM ET the exchange cancels all orders and pauses;
the bot handles that itself.

## Optional: start automatically when you log in
Task Scheduler → Create Task → *Triggers*: At log on → *Actions*: Start a program:
`powershell.exe` with arguments
`-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\btc-signals\windows\start-bot.ps1"`.
Only for **paper** mode; LIVE always needs you to start it in a window and type the confirmation.

## Troubleshooting
| Problem | Fix |
|---|---|
| `running scripts is disabled on this system` | use the `.bat` files, or `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `py` not found | reinstall Python with "Add python.exe to PATH", open a new PowerShell |
| port 8000 in use | `.\.venv\Scripts\python.exe -m app.main --port 8001` |
| start-bot says it didn't start | read `data\console.log` (e.g. LIVE mode refuses to start hidden: use `-Visible`) |
| browser shows "can't provide a secure connection" | start with `-Https` and open https://localhost:8000 |
