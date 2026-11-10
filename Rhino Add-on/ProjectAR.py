#! python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Opens the ProjectAR window in Rhino 8.

Run it with RunPythonScript, or from the Script Editor. Running it again
brings the same window back: the server keeps running while the window is
closed. Keep this file next to the projectar folder.

To start streaming whenever Rhino opens, add this line to Rhino's startup
commands (Options > General > Command Lists):

    _-RunPythonScript "<path to this folder>/ProjectAR.py"
"""

import os
import sys

try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # run from somewhere that doesn't set __file__
    HERE = os.getcwd()
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import projectar.app  # noqa: E402

projectar.app.show()
