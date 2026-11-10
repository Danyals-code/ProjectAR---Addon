# SPDX-License-Identifier: GPL-3.0-or-later
"""Settings kept between Rhino sessions, in a small JSON file:
%APPDATA%\\ProjectAR on Windows, ~/Library/Application Support/ProjectAR on
macOS. Choices for the current session (what to stream, pausing) aren't
saved, as in the Blender add-on."""

import json
import os
import sys

DEFAULTS = {
    "port": 51515,
    "update_rate": 30,
    "max_triangles": 300_000,
    "max_instances": 2000,
    "max_texture_size": 1024,
    "auto_start": False,
    "require_approval": True,
    "trusted_devices": {},  # device key -> name
    "study_folder": "",
    "study_tasks": "",
    "study_counterbalance": True,
}
LIMITS = {
    "port": (1024, 65535),
    "update_rate": (5, 60),
    "max_triangles": (1_000, 5_000_000),
    "max_instances": (0, 10_000),
}
TEXTURE_SIZES = (512, 1024, 2048)


def folder():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ProjectAR")


def path():
    return os.path.join(folder(), "rhino-settings.json")


def clean(values):
    """Settings with every value present and of the right kind."""
    out = dict(DEFAULTS)
    for name, default in DEFAULTS.items():
        value = values.get(name, default) if isinstance(values, dict) else default
        if isinstance(default, bool):
            out[name] = bool(value)
        elif isinstance(default, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            low, high = LIMITS.get(name, (None, None))
            if low is not None:
                value = min(max(value, low), high)
            if name == "max_texture_size" and value not in TEXTURE_SIZES:
                value = default
            out[name] = value
        elif isinstance(default, dict):
            out[name] = {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
        else:
            out[name] = str(value or "")
    return out


def load():
    try:
        with open(path(), encoding="utf-8") as f:
            return clean(json.load(f))
    except (OSError, ValueError):
        return dict(DEFAULTS, trusted_devices={})


def save(values):
    try:
        os.makedirs(folder(), exist_ok=True)
        temp = path() + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(clean(values), f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp, path())
    except OSError as exc:
        print(f"ProjectAR: could not save settings: {exc}")
