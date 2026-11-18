"""Support ``python -m kgov`` as an entry point into the CLI.

Keeps the package executable the same three ways: the installed ``kgov``
console script, ``python -m kgov.cli``, and ``python -m kgov``.
"""

import sys

from kgov.cli import main

if __name__ == "__main__":
    sys.exit(main())
