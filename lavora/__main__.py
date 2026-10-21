"""Entry point. Lavora.exe starts the GUI, lavora-cli.exe the CLI (chosen by exe name, like Zapi); from source,
`python -m lavora` opens the GUI and `python -m lavora <command>` runs the CLI."""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        from .selftest import run
        return run()
    exe = os.path.splitext(os.path.basename(sys.argv[0]))[0].lower()
    frozen = getattr(sys, "frozen", False)
    if exe.endswith("-cli") or (not frozen and len(sys.argv) > 1):
        from .cli.main import main as cli_main
        return cli_main()
    from .gui.app import run as gui_run
    return gui_run()


if __name__ == "__main__":
    raise SystemExit(main())
