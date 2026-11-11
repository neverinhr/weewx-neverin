"""The extension's module is bin/user/neverin.py, as WeeWX installs it; the tests import it from there."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin" / "user"))
