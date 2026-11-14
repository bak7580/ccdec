"""Double-click launcher for the ccdec window (no console).

If nothing appears, run "python -m ccdec.gui" in a terminal to see the error.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ccdec.gui import main  # noqa: E402

sys.exit(main())
