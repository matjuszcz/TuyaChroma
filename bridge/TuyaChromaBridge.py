"""Entry point for PyInstaller / running from source."""

import sys

from tuyachroma_bridge.console import setup_console

setup_console()  # before tinytuya/colorama wrap stdout

from tuyachroma_bridge.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
