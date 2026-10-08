"""Share this PC with a 2010-era Samsung TV."""

from __future__ import annotations

import sys


def main() -> None:
    if "--check" in sys.argv:
        from tvstreamer.check import run_check

        sys.exit(run_check())
    from tvstreamer.app import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
