from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--tun-helper":
        # Must not import Qt: this runs with elevated privileges.
        from .tun import helper

        code = helper.run(argv[1:])
        sys.stdout.flush()
        os._exit(code)
    if argv and argv[0] in ("-V", "--version"):
        from . import __version__

        print(f"cqta {__version__}")
        return 0
    from .gui.app import run

    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
