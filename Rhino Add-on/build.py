#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Package the Rhino add-on into dist/projectar-rhino-<version>.zip: the
ProjectAR.py launcher and the projectar folder, ready to unzip anywhere.

    python3 build.py          build the zip
    python3 build.py --test   run the tests first, and only build if they pass
"""

import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "projectar"
DIST = ROOT / "dist"
FOLDER = "ProjectAR for Rhino"


def version():
    text = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        sys.exit("build.py: no VERSION in projectar/__init__.py")
    return match.group(1)


def included(path):
    rel = path.relative_to(ROOT)
    return path.is_file() and not any(
        part.startswith(".") or part == "__pycache__" for part in rel.parts
    ) and path.suffix != ".pyc"


def build():
    DIST.mkdir(exist_ok=True)
    for old in DIST.glob("projectar-rhino-*.zip"):
        old.unlink()
    target = DIST / f"projectar-rhino-{version()}.zip"
    files = [ROOT / "ProjectAR.py", ROOT / "README.md"] + sorted(p for p in PACKAGE.rglob("*") if included(p))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, f"{FOLDER}/{path.relative_to(ROOT).as_posix()}")
    return target


if __name__ == "__main__":
    if "--test" in sys.argv:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT)
        if result.returncode:
            sys.exit("build.py: tests failed, nothing built")
    print(f"Built {build().relative_to(ROOT)}")
