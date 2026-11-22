"""Where the emulator tools may keep their state folder (--state)."""
import os
import sys
import tempfile
from pathlib import Path


def safe_state_root(arg: str) -> Path:
    """The folder named by --state, resolved. It has to be inside the current folder, the user's home folder
    or the temp folder, so a mistyped or hostile value cannot point the tools at system folders."""
    path = os.path.realpath(arg)
    for allowed in (os.getcwd(), os.path.expanduser("~"), tempfile.gettempdir()):
        root = os.path.realpath(allowed)
        try:
            inside = os.path.commonpath([path, root]) == root
        except ValueError:                       # different drives on Windows
            continue
        if inside:
            return Path(path)
    sys.exit(f"--state {arg!r} must be inside the current folder, your home folder or the temp folder")
