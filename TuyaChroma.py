"""Entry point for PyInstaller / running from source."""

import sys

from tuyachroma.console import setup_console

setup_console()

from tuyachroma.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
