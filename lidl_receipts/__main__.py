"""Allow `python3 -m lidl_receipts <command>`."""

import sys

from .cli import main

sys.exit(main())
