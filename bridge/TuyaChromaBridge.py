"""Entry point for PyInstaller / running from source."""

import sys

from tuyachroma_bridge.app import main

if __name__ == "__main__":
    sys.exit(main())
