# Third-party source and assets

This project is GPL-3.0-or-later. A copy is included in LICENSE. Modified vendored
files carry notices identifying the adaptation. No upstream GUI code is included.

## turing-smart-screen-python

- Source: https://github.com/mathoudebine/turing-smart-screen-python
- Local snapshot: `bd95bc388c4ea780436b3d0e11af2fe7b2283d5e` plus workspace dashboard changes.
- Copyright: Matthieu Houdebine and contributors; original notices retained.
- License: GPL-3.0-or-later.
- Reused: `library/codex_icon.py`, `library/dashboard_chart.py`,
  `library/sensors/ai_dashboard.py`, `library/sensors/codex_usage.py`,
  `library/sensors/new_api_setup.py`, and the `check-new-api.py` workflow,
  `res/themes/AI-Dashboard/{theme.yaml,background.png}`, and Codex usage regression tests.
- Changes: imports moved into `turzx_dashboard._vendor.turing`; test asset paths
  relocated. The new frame composer and animation are outside the vendored renderer.

## Libre Panel

- Source: https://github.com/syntensa/libre-panel
- Snapshot: `124bbbd7d2ecb6536f8ba9f2a51663c691adb27a` (0.6.4).
- Copyright: Libre Panel contributors; protocol work also credits the SPUR II project
  and turing-smart-screen-python, as retained in source comments.
- License: GPL-3.0-or-later.
- Reused: USB transport, video session and recovery, H.264 encoder, model catalog,
  timing guards, bundled-program lookup, display interface, configuration dataclasses,
  and the simulated USB panel from its tests.
- Changes: imports moved into `turzx_dashboard._vendor.libre_panel`; display factories
  and unrelated configuration omitted; English message formatting replaces app
  localization; CLI dependency/configuration messages updated for this project.
- Protocol reference: https://github.com/syntensa/libre-panel/blob/main/docs/protocol/turzx-usb.md

## Fonts

Roboto Bold, Roboto Black and Roboto Mono Regular are copied from
turing-smart-screen-python. Their Apache-2.0 license texts are included beside the
font files in `src/turzx_dashboard/assets/fonts/roboto/LICENSE.txt` and
`src/turzx_dashboard/assets/fonts/roboto-mono/LICENSE.txt`.

FFmpeg is an external runtime dependency and is not bundled in the distributed
project. The development/test dependency imageio-ffmpeg supplies a test executable
under its own distribution terms.
