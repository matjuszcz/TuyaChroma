import sys

from tuyachroma.console import setup_console

setup_console()

from tuyachroma.main import main  # noqa: E402

sys.exit(main())
