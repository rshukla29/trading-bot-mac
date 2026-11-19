"""A macOS notification (Notification Centre), no extra packages: osascript's
`display notification`. The title and text are passed as arguments, never
pasted into the script, so no quoting can break it."""
from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

_SCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
           "-e", "end run"]


def mac_notify(title: str, text: str) -> bool:
    """Show it (in the background); False when not on macOS or it failed."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.Popen(["osascript", *_SCRIPT, title, text], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        logger.exception("Couldn't show the notification")
        return False
    return True
