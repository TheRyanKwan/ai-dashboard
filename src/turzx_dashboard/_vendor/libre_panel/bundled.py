# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: imports relocated; see THIRD_PARTY.md.
"""The programs that come with a download, in a ``tools`` folder next to Libre Panel.

The Windows setup and zip carry ffmpeg (the video mode), PresentMon (game frame rates) and
PawnIO's installer (the CPU's sensors); the Linux AppImage and tarball carry ffmpeg.
``packaging/fetch_tools.py`` fetches them when a download is built, ``tools/SOURCES.txt`` and
NOTICE.md say where they come from. A pip install has no such folder: it uses what the
system has, and ``video.ffmpeg`` or ``[sensors.presentmon] path`` name the rest.

Only a release build looks here, and only beside its own program: whoever may write there
may replace Libre Panel itself. Never the user's Downloads.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

TOOLS_FOLDER = "tools"  # in the downloads, next to the program's own files

# PawnIO's installer is run with administrator rights, from a folder that the user who
# installed Libre Panel may be able to write to: it is run only if it is the file that was
# fetched. This is the SHA-256 that packaging/fetch_tools.py pins (a test keeps the two equal).
PAWNIO_SETUP = "PawnIO_setup.exe"
PAWNIO_SETUP_SHA256 = "1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032"


def tools_folders() -> list[Path]:
    """The folders a download keeps its programs in, the one beside the program first. None
    for a pip install or a checkout, which carry no programs."""
    if not getattr(sys, "frozen", False):
        return []
    folders = [Path(sys.executable).resolve().parent / TOOLS_FOLDER]
    bundle = getattr(sys, "_MEIPASS", None)  # PyInstaller's own folder (_internal)
    if bundle and (inside := Path(bundle) / TOOLS_FOLDER) not in folders:
        folders.append(inside)
    return folders


def program_name(name: str) -> str:
    """The file name of a program here: ``ffmpeg`` is ``ffmpeg.exe`` on Windows."""
    return f"{name}.exe" if sys.platform == "win32" else name


def find(file_name: str) -> str | None:
    """The path of the file ``file_name`` that the download carries, or None."""
    for folder in tools_folders():
        path = folder / file_name
        if path.is_file():
            return str(path)
    return None


def pawnio_installer() -> str | None:
    """The path of PawnIO's installer, if the download carries it and it is the file that was
    fetched; None if it is missing or has been replaced. Whoever runs it with administrator
    rights copies it to a folder only administrators may write to first, and checks the copy."""
    path = find(PAWNIO_SETUP)
    if path is None:
        return None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    return path if digest == PAWNIO_SETUP_SHA256 else None
