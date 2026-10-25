# SPDX-License-Identifier: GPL-3.0-or-later
"""Image textures of Principled BSDF materials, for apps that ask for them.

Materials name their textures by id under "textures" in the material info.
Each texture is sent once per device as a kind 3 frame: a little-endian
uint32 header length, a JSON header {"id", "format"}, then PNG or JPEG bytes.

Ids depend on the image's name, file and size, so an edited image file gets a
new id. Images larger than the size limit are scaled down first.
"""

import hashlib
import json
import os
import struct
import zlib

import bpy
import numpy as np

from .server import encode_frame

KIND_TEXTURE = 3
FEATURE = "textures"

# Principled BSDF input -> role in the app.
ROLES = {
    "Base Color": "baseColor",
    "Roughness": "roughness",
    "Metallic": "metallic",
}
_FILE_FORMATS = {"PNG": "png", "JPEG": "jpeg"}


def _linked_node(socket, depth=0):
    """The node feeding a socket, looking through reroutes."""
    if not socket.is_linked or depth > 8:
        return None, None
    link = socket.links[0]
    if not link.is_valid or link.is_muted:
        return None, None
    node = link.from_node
    if node.type == "REROUTE":
        return _linked_node(node.inputs[0], depth + 1)
    return node, link.from_socket


def _image_node(socket):
    node, out = _linked_node(socket)
    if node is not None and node.type == "TEX_IMAGE" and node.image is not None:
        return node, out
    return None, None


def texture_id(image, role):
    """Stable for an image, different when its file or size changes. Painted
    pixels keep the id and are resent under it. The role is part of it, since
    one image used as both color and roughness is sent twice, encoded
    differently."""
    try:
        size = tuple(image.size)
    except (RuntimeError, ReferenceError):
        size = (0, 0)
    packed = image.packed_file.size if image.packed_file else 0
    path = bpy.path.abspath(image.filepath) if image.filepath else ""
    mtime = 0
    if path and not packed:
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            pass
    key = f"{image.name_full}|{path}|{size}|{packed}|{mtime}|{image.source}|{role}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def material_textures(principled):
    """{role: image} for a Principled BSDF, plus whether alpha comes from the
    base color image."""
    found = {}
    alpha_from_texture = False
    for socket_name, role in ROLES.items():
        socket = principled.inputs.get(socket_name)
        if socket is None:
            continue
        node, _ = _image_node(socket)
        if node is not None:
            found[role] = node.image
    normal_socket = principled.inputs.get("Normal")
    if normal_socket is not None:
        node, _ = _linked_node(normal_socket)
        if node is not None and node.type == "NORMAL_MAP":
            image_node, _ = _image_node(node.inputs["Color"])
            if image_node is not None:
                found["normal"] = image_node.image
    alpha_socket = principled.inputs.get("Alpha")
    if alpha_socket is not None and "baseColor" in found:
        node, out = _image_node(alpha_socket)
        alpha_from_texture = node is not None and node.image == found["baseColor"] and out.name == "Alpha"
    return found, alpha_from_texture


class TextureStore:
    """Encodes textures once and remembers which image each id is."""

    def __init__(self, max_size):
        self.max_size = max_size
        self._images = {}  # id -> (image name, role)
        self._frames = {}  # id -> encoded frame, or None if it failed
        self._edited = set()  # images painted or changed in Blender
        self._live = set()  # ids registered since the last epoch

    def register(self, image, role):
        tid = texture_id(image, role)
        if tid not in self._images:
            self._images[tid] = (image.name_full, role)
        self._live.add(tid)
        return tid

    def begin_epoch(self):
        """Starts a fresh count of the ids in use. Ids not registered again
        are forgotten, with their encoded frames."""
        stale = [tid for tid in self._images if tid not in self._live]
        for tid in stale:
            self._images.pop(tid, None)
            self._frames.pop(tid, None)
        self._live = set()

    def pixels_changed(self, name):
        """An image was painted or edited in Blender. Its frames are encoded
        again from its pixels, and the ids returned need resending."""
        self._edited.add(name)
        changed = [tid for tid, (image, _) in self._images.items() if image == name]
        for tid in changed:
            self._frames.pop(tid, None)
        return changed

    def set_max_size(self, max_size):
        if max_size != self.max_size:
            self.max_size = max_size
            self._frames.clear()

    def ids(self):
        """Texture ids in use, in the order materials first used them."""
        return list(self._images)

    def known(self, tid):
        return tid in self._images

    def ready(self, tid):
        return tid in self._frames

    def frame(self, tid):
        """The kind 3 frame for an id, encoding it now if needed. None if the
        image is gone or can't be read."""
        if tid not in self._frames:
            name, role = self._images.get(tid, (None, None))
            image = _find_image(name) if name else None
            data = None
            if image is not None:
                try:
                    # Once edited in Blender, the file on disk may be out of date.
                    data = encode_image(image, role, self.max_size, from_file=name not in self._edited)
                except Exception as exc:  # one bad image shouldn't stop streaming
                    print(f"ProjectAR: could not send texture {name}: {exc}")
            self._frames[tid] = _texture_frame(tid, *data) if data else None
        return self._frames[tid]


def _find_image(name_full):
    """An image by its full name, which for linked images includes the library."""
    image = bpy.data.images.get(name_full)
    if image is not None and image.name_full == name_full:
        return image
    return next((i for i in bpy.data.images if i.name_full == name_full), None)


def _texture_frame(tid, fmt, data):
    header = json.dumps({"id": tid, "format": fmt}, separators=(",", ":")).encode("utf-8")
    return encode_frame(KIND_TEXTURE, struct.pack("<I", len(header)) + header + data)


def encode_image(image, role, max_size, from_file=True):
    """(format, bytes) for an image, at most max_size on its longest side."""
    width, height = image.size
    if width == 0 or height == 0:
        return None
    # Files that already fit are sent as they are.
    # Unsaved paint lives only in memory, so dirty images are read from pixels.
    if (
        from_file
        and max(width, height) <= max_size
        and image.source == "FILE"
        and image.file_format in _FILE_FORMATS
        and not image.is_dirty
    ):
        raw = None
        if image.packed_file is not None:
            raw = bytes(image.packed_file.data)
        else:
            path = bpy.path.abspath(image.filepath)
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    raw = f.read()
        if raw:
            return _FILE_FORMATS[image.file_format], raw

    if from_file and not image.is_dirty:
        # Untouched images: let Blender scale a copy, which is fast.
        scale = min(1.0, max_size / max(width, height))
        w, h = max(1, round(width * scale)), max(1, round(height * scale))
        source = image
        copy = None
        if (w, h) != (width, height):
            copy = image.copy()
            copy.scale(w, h)
            source = copy
        try:
            pixels = np.empty(w * h * 4, dtype=np.float32)
            source.pixels.foreach_get(pixels)
        finally:
            if copy is not None:
                bpy.data.images.remove(copy)
    else:
        # A copy would reload the file and lose unsaved paint, so average
        # blocks of the image's own pixels instead.
        full = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(full)
        full = full.reshape(height, width, 4)
        factor = max(1, -(-max(width, height) // max_size))
        # Each side gets its own factor, so a strip only one or two pixels
        # thin keeps its short side instead of failing to reshape.
        fy, fx = min(factor, height), min(factor, width)
        h, w = height // fy, width // fx
        if fy > 1 or fx > 1:
            full = full[: h * fy, : w * fx].reshape(h, fy, w, fx, 4).mean(axis=(1, 3))
        pixels = full
    pixels = pixels.reshape(h, w, 4)[::-1]  # Blender stores rows bottom up
    if image.is_float and role == "baseColor" and image.colorspace_settings.name != "Non-Color":
        rgb = np.clip(pixels[..., :3], 0.0, 1.0)
        pixels[..., :3] = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
    data = (np.clip(pixels, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if role in ("roughness", "metallic"):
        gray = (data[..., 0].astype(np.uint16) + data[..., 1] + data[..., 2]) // 3
        return "png", _png(gray.astype(np.uint8)[..., None])
    if role == "baseColor" and data[..., 3].min() < 255:
        return "png", _png(data)
    return "png", _png(np.ascontiguousarray(data[..., :3]))


def _png(pixels):
    """A PNG from an (h, w, channels) uint8 array, channels 1, 3 or 4."""
    h, w, channels = pixels.shape
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    rows = np.empty((h, 1 + w * channels), dtype=np.uint8)
    rows[:, 0] = 0  # no filter
    rows[:, 1:] = pixels.reshape(h, w * channels)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)),
        chunk(b"IEND", b""),
    ])
