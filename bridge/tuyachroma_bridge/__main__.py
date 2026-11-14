import sys

from tuyachroma_bridge.console import setup_console

setup_console()

from tuyachroma_bridge.app import main  # noqa: E402

sys.exit(main())
