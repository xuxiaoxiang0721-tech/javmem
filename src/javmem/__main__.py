"""让 `python -m javmem` 和 `javmem` 命令一样用。"""

import sys

from javmem.cli import main

sys.exit(main())
