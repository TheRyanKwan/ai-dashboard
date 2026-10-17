# TURZX Dashboard

A standalone Python CLI for the **TURZX 9.2-inch USB screen (`1cbe:0092`)**. It combines
the existing **AI-Dashboard** artwork, New API account data, and Codex session tracking
from turing-smart-screen-python with Libre Panel's H.264 video transport.

No tray, browser, theme editor, Tk, or desktop session is required. Neither sibling
checkout is needed after installation. Python 3.11+ is required.

## Install

From this directory, on macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[usb]'
```

On Windows (PowerShell):

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install ".[usb]"
```

Install **FFmpeg with the `libx264` encoder** for video. On macOS, Homebrew supplies
both native dependencies:

```sh
brew install ffmpeg libusb
```

On Debian/Ubuntu:

```sh
sudo apt install ffmpeg libusb-1.0-0
```

Windows USB installations include a libusb backend through `libusb-package`. Install
FFmpeg on PATH, or set `display.ffmpeg` to its executable. Close any other software
using the screen before connecting. `python -m turzx_dashboard` is also supported.

## Preview without a screen

Start a live browser preview at **http://localhost:5678/**:

```sh
turzx-dashboard serve
```

It streams the animated dashboard at the configured frame rate (default 30 fps),
respecting `DEMO` in `dashboard.local.yaml` (default `false`). To use labeled sample
data and cycle Idle, Working, and Done, run `turzx-dashboard serve --demo`.
`--demo` and `--live` explicitly override the configured mode. It does not access
USB or need FFmpeg. The page scales to the browser width and has a pause/resume
button; reduced-motion preferences start it paused. Ctrl+C stops the server.
`/frame.png` (also `/screencap.png`) returns a current PNG snapshot.

To show your real account and local Codex data, use:

```sh
turzx-dashboard serve --live --config dashboard.local.yaml
```

`--port` changes the port, and `--host` changes the listen address (default loopback).
The preview has no authentication; bind it to a shared network only if you intend
to share the visible dashboard. `--state done` pins an expression for inspection.

For file previews and captures:

```sh
turzx-dashboard preview --state working --output output/preview.png
turzx-dashboard preview --state done --output output/done.gif
turzx-dashboard run --simulate --demo --duration 20
```

`preview` uses labeled sample account data by default and makes no network requests.
Use `preview --live` explicitly to read your configured account. Animated GIFs are
limited to 10 seconds and 15 fps; playback to the panel supports 1–50 fps (default 30).
The simulator updates `output/frame.png` atomically four times a second. It runs until
Ctrl+C unless `--duration` is supplied. Demo playback cycles Idle → Working → Done
every six seconds; `--state` pins a state for testing.

Soft peach, lavender, and blue meteors shoot diagonally across the open background,
with fading trails behind the artwork. The lines around the balance circle orbit
slowly in opposite directions.
`--reduced-motion` keeps the artwork static. The changing state remains visible in
words and expressions. Account figures and Codex token/cost figures stay still during
animation.

## Configure live data

Copy `dashboard.example.yaml` to **`dashboard.local.yaml`** and edit it. This filename
is ignored by Git. Paths supplied to `--config` may use any name; keep other credential
files out of version control yourself. No credentials were copied from the sibling projects.

Both **flat** and wrapped configuration mappings are supported:

```sh
turzx-dashboard run --config dashboard.local.yaml --simulate
```

The new example wraps those same settings under `dashboard:` and adds `display:`
settings. `NEW_API_URL`, `NEW_API_ACCESS_TOKEN`, `NEW_API_COOKIE`, and
`NEW_API_USER_ID` environment variables override the corresponding account values.
`.env` files are not loaded automatically. `--demo` and `--live` override `DEMO`.

Check account access from this project's directory without displaying credentials
or balances:

```sh
.venv/bin/python check-new-api.py
```

To create a management token, set `NEW_API_URL`, `NEW_API_COOKIE`, and
`NEW_API_USER_ID` in `dashboard.local.yaml` or your environment, then run:

```sh
.venv/bin/python check-new-api.py --create-token
```

This verifies the browser account, calls the legacy `/api/user/token` endpoint
once, saves the new token atomically in this project's configuration, and verifies
it. The endpoint may invalidate the previous token; it is never retried
automatically. Both flat and wrapped YAML retain their other settings. Restart the
dashboard after saving; exported account variables still override the file.
`--config PATH` selects another configuration file. Installed wheels also support
`python -m turzx_dashboard.check_new_api` with the same arguments. On Windows use
`.venv\Scripts\python.exe` in place of `.venv/bin/python`.

The balance is converted with `QUOTA_PER_RMB`; daily totals include **all** calls
today, while the chart shows the latest 60 calls. The day defaults to UTC+8.
Independent background workers poll the balance and full daily totals every 20 seconds,
the latest chart page every 2 seconds, and local Codex events every half second.
Network requests never run on the animation thread. Failed collections retain prior
values with a visible stale notice, and incomplete pagination is never treated as a
complete daily total.

## Connect the screen

Start with the PNG transport to verify USB access without video startup:

```sh
turzx-dashboard run --demo --still --duration 10
```

Video startup needs the **existing standby clip name on your panel**. Supply that
value as `display.clip`. A common vendor value is `usr/data/standby.h264`, but do not assume it is
your panel's value. The application refuses video startup with an empty name.

Once configured:

```sh
turzx-dashboard doctor
turzx-dashboard run --demo --duration 15
turzx-dashboard run --live
```

Or supply settings directly, replacing the placeholder with your existing clip name:

```sh
turzx-dashboard run --demo --clip "YOUR_EXISTING_CLIP_NAME" --fps 30 --duration 15
```

The application streams generated frames; it does not download GIFs or external video.
It does not upload files, write firmware, or save panel settings. Video startup sends
the existing clip name into the player's volatile state. Shutdown stops the stream,
restores the normal playback rate, and leaves a sharp PNG of the last frame.
The inherited backend can restart a hung decoder; USB failures retry every three
seconds, and unplugging/replugging resumes playback. A finite run with no successfully
submitted frames exits unsuccessfully.

`doctor` checks Python, FFmpeg/libx264, libusb, USB command encryption, clip configuration,
and whether `1cbe:0092` is present, without claiming the screen or contacting your account.
It returns nonzero when video prerequisites are missing. Device presence does not prove
permission to claim it; use the short PNG test above to check that.

On Linux, if USB access is denied, install this udev rule, reload rules, and replug:

```sh
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="1cbe", ATTR{idProduct}=="0092", MODE="0660", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-turzx-dashboard.rules
sudo udevadm control --reload-rules
```

The logical layout remains 1920×480, with content in its first 462 rows. The video
driver uses the physical 480×1920 framebuffer, whose first 18 landscape rows are
hidden. `display.y_offset: 18` compensates for that; `--y-offset 0` disables compensation.
`--rotate 180` supports an inverted mount. These alignment assumptions need checking
on your individual panel.

## Codex integration

Use this project's `notify` command for all Codex events. No scripts, environments,
configuration, or assets from another project are required. Events default to
`~/.cache/turzx-dashboard/codex-event.json`, with separate records for each session.
Local Codex transcripts supply session selection, cached-input subtraction, task
counts, and explicitly configured model prices. Forked sessions load inherited usage
from their parent transcript up to the recorded fork boundary; keep those transcripts
available in `CODEX_SESSIONS_DIR` (by default, `~/.codex/sessions`).

The dashboard also detects Working, Done, and Idle directly from local transcript
lifecycle events. This supports Codex clients that do not deliver start hooks.
It follows the 128 most recently modified transcripts (or the pinned
`CODEX_SESSION_ID`), reading new records on each poll. The newest lifecycle event
for each session wins when both transcripts and hooks are available.

Costs require `CODEX_MODEL_RATES` and `CODEX_COST_CURRENCY` in this project's
configuration. Rates are prices **per million tokens**, in the chosen currency,
for `INPUT`, `CACHE_READ`, `CACHE_WRITE`, and `OUTPUT`. Use `"*"` for default
rates or an exact model name for an override. `INPUT` applies to input excluding
cache reads and writes; output already includes reasoning tokens. See the commented
example in `dashboard.example.yaml` and replace its sample values with your provider's
billing rates. Restart the dashboard after changing configuration. Unknown usage,
missing parent history, or missing prices display an em dash.

To check the event bridge without changing live state:

```sh
turzx-dashboard notify --test --event-file output/test-event.json
```

Configure Codex's `notify` array in `~/.codex/config.toml` using the absolute Python
path from this project's environment (on Windows, use `.venv/Scripts/python.exe`):

```toml
notify = ["/absolute/path/to/turzx-dashboard/.venv/bin/python", "-m", "turzx_dashboard", "notify"]
```

In `~/.codex/hooks.json`, use that same Python executable with
`-m turzx_dashboard notify --hook` as the command for `UserPromptSubmit`,
`Interrupt`, and `SessionEnd`. `UserPromptSubmit` sets Working, completion through
`notify` sets Done for `CODEX_DONE_SECONDS` (default 30 seconds), and interruption
sets Idle. Restart Codex after changing its integration configuration and review
any hook trust prompt it presents.

`turzx-dashboard notify --hook` reads lifecycle JSON from stdin;
`turzx-dashboard notify 'JSON_PAYLOAD'` accepts completion JSON.
`--event-file` selects the destination, and the dashboard's `CODEX_EVENT_FILE` must
match it. The `CODEX_EVENT_FILE` environment variable takes precedence in both.
The command writes local event records only and never opens USB or makes requests.

## Validation and limits

```sh
python -m pip install -e '.[usb,dev]'
python -m pytest
ruff check src tests
python -m build
```

Tests cover original Codex transcript accounting, live account HTTP pagination and
stale recovery, offline rendering, motion with stable figures, framebuffer mapping,
CLI notifications, shutdown, reconnection, and **real FFmpeg encoding/decoding through
a simulated USB panel**. The dev extra provides an FFmpeg binary for these tests.
A CI workflow defines Windows, macOS, and Linux checks with Python 3.11 and 3.14.

This implementation was tested in the current Linux environment. No physical screen
was attached for validation; macOS and Windows hardware playback remain unverified.
The 50 fps figure comes from Libre Panel's measurements, not a promise of this
application's performance on every machine. Start at 25–30 fps.

Licensed GPL-3.0-or-later; vendored sources and font licenses are recorded in
[THIRD_PARTY.md](THIRD_PARTY.md).
