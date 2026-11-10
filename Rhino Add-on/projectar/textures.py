# SPDX-License-Identifier: GPL-3.0-or-later
"""Image textures of materials, for apps that ask for them.

Materials name their textures by id under "textures" in the material info.
Each texture is sent once per device as a kind 3 frame: a little-endian
uint32 header length, a JSON header {"id", "format"}, then PNG or JPEG bytes.

Ids depend on the image file, its size on disk and when it changed, so an
edited image gets a new id. Reading and scaling images is the host's job:
the store calls its loader, so this module needs nothing from Rhino.
"""

import hashlib
import json
import struct

from .server import encode_frame

KIND_TEXTURE = 3
FEATURE = "textures"
ROLES = ("baseColor", "normal", "roughness", "metallic")


def texture_id(source, role):
    """Stable for one image file in one role. The role is part of it, since
    one image used for color and roughness is sent twice."""
    return hashlib.sha1(f"{source!r}|{role}".encode("utf-8")).hexdigest()[:16]


def image_format(data):
    """"png" or "jpeg" from the first bytes, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


def image_size(data):
    """(width, height) of a PNG or JPEG without decoding it, or None."""
    fmt = image_format(data)
    if fmt == "png":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        return None
    if fmt == "jpeg":
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker == 0xFF:
                i += 1
                continue
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            # Start of frame markers, except DHT (C4), JPG (C8) and DAC (CC).
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 > len(data):
                    return None
                height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                return width, height
            i += 2 + length
    return None


def texture_frame(tid, fmt, data):
    header = json.dumps({"id": tid, "format": fmt}, separators=(",", ":")).encode("utf-8")
    return encode_frame(KIND_TEXTURE, struct.pack("<I", len(header)) + header + data)


class TextureStore:
    """Encodes textures once and remembers which image each id is.

    loader(source, role, max_size) returns (format, bytes) at most max_size
    on the longest side, or None when the image can't be read."""

    def __init__(self, max_size, loader):
        self.max_size = max_size
        self._loader = loader
        self._sources = {}  # id -> (source, role)
        self._data = {}  # id -> (format, bytes), or None if it failed
        self._frames = {}  # id -> encoded frame, or None
        self._live = set()  # ids registered since the last epoch

    def register(self, source, role):
        tid = texture_id(source, role)
        if tid not in self._sources:
            self._sources[tid] = (source, role)
        self._live.add(tid)
        return tid

    def begin_epoch(self):
        """Starts a fresh count of the ids in use. Ids not registered again
        are forgotten, with their encoded frames."""
        for tid in [t for t in self._sources if t not in self._live]:
            self._sources.pop(tid, None)
            self._data.pop(tid, None)
            self._frames.pop(tid, None)
        self._live = set()

    def set_max_size(self, max_size):
        if max_size != self.max_size:
            self.max_size = max_size
            self._data.clear()
            self._frames.clear()

    def ids(self):
        """Texture ids in use, in the order materials first used them."""
        return list(self._sources)

    def known(self, tid):
        return tid in self._sources

    def ready(self, tid):
        return tid in self._frames

    def data(self, tid):
        """(format, bytes) for an id, loading it now if needed, or None."""
        if tid not in self._data:
            source, role = self._sources.get(tid, (None, None))
            result = None
            if source is not None:
                try:
                    result = self._loader(source, role, self.max_size)
                except Exception as exc:  # one bad image shouldn't stop streaming
                    print(f"ProjectAR: could not read texture {source}: {exc}")
            self._data[tid] = result
        return self._data[tid]

    def frame(self, tid):
        """The kind 3 frame for an id, or None if the image can't be read."""
        if tid not in self._frames:
            result = self.data(tid)
            self._frames[tid] = texture_frame(tid, *result) if result else None
        return self._frames[tid]
