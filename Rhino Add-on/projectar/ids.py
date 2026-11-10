# SPDX-License-Identifier: GPL-3.0-or-later
"""Readable, stable ids for Rhino objects.

The app shows an object's id as its name, so a GUID won't do. Each object
gets its Rhino name, or a name for its type when it has none ("Polysurface"),
and a number when that's taken ("Chair 2"), the way Blender names copies.

An id stays the same for the whole Rhino session, so the app never sees an
object disappear and come back because another object took its name. It
changes when the object is renamed, which the app sees as a new object, as
it does for a renamed Blender object.
"""

from .protocol import readable_id


class IdRegistry:
    def __init__(self):
        self._by_key = {}  # key -> (id, base name)
        self._taken = {}  # id -> key

    def label(self, key, name):
        """The id for a key (such as an object's GUID), from its preferred name."""
        base = readable_id(name)
        current = self._by_key.get(key)
        if current is not None:
            if current[1] == base:
                return current[0]
            del self._taken[current[0]]  # renamed: the old id is free again
        label, number = base, 2
        while label in self._taken:
            label = f"{base} {number}"
            number += 1
        self._by_key[key] = (label, base)
        self._taken[label] = key
        return label

    def get(self, key):
        found = self._by_key.get(key)
        return found[0] if found else None

    def key_for(self, label):
        """The key an id was given for, or None."""
        return self._taken.get(label)

    def clear(self):
        self._by_key.clear()
        self._taken.clear()
