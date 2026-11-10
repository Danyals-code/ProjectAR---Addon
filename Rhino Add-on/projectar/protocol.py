# SPDX-License-Identifier: GPL-3.0-or-later
"""The ProjectAR wire protocol, without anything from Rhino.

Frames, JSON messages, mesh payloads and the axis conversion are exactly the
Blender add-on's (its specification, section 4, and the app's developer guide,
section 4.8), so the app can't tell the two apart.

Matrices here are 4x4 lists of rows acting on column vectors, like Rhino's
Transform. On the wire they are 16 floats, column by column.
"""

import json
import math
import re
import struct
from dataclasses import dataclass, field

from .server import KIND_JSON, KIND_MESH, encode_frame

PROTOCOL_VERSION = 1
# Optional extras listed in "hello". The app only uses what's listed here.
FEATURES = [
    "setMaterial", "variants", "notes", "roomScan", "decisions", "textures", "pairing",
    "materialDefinitions", "materialUpdates", "instancing", "lights", "cameras", "setCamera", "snapshot",
]
# What an app lists in "client" when it can read messages older apps can't.
CLIENT_MATERIAL_UPDATES = "materialUpdates"
CLIENT_INSTANCING = "instancing"
CLIENT_TEXTURES = "textures"
CLIENT_SNAPSHOT = "snapshot"
PROTOTYPE_PREFIX = "~mesh/"
HOST = "Rhino"

DEFAULT_MATERIAL = {
    "name": "Default",
    "color": [0.8, 0.8, 0.8, 1.0],
    "metallic": 0.0,
    "roughness": 0.5,
}


# ---------------------------------------------------------------- JSON


def finite(value):
    """The value with any NaN or infinity replaced by 0, which JSON can't carry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def json_frame(message):
    try:
        data = json.dumps(message, separators=(",", ":"), allow_nan=False)
    except ValueError:
        # The app rejects NaN, so a damaged value must not take the whole message with it.
        data = json.dumps(finite(message), separators=(",", ":"), allow_nan=False)
    return encode_frame(KIND_JSON, data.encode("utf-8"))


def key(value):
    """A comparable text for a message part, to notice changes."""
    return json.dumps(finite(value), sort_keys=True, separators=(",", ":"))


def text(value, limit):
    """Text from the app, made safe to store: lone surrogates in JSON become
    replacement characters instead of errors when the file is saved."""
    return str(value if value is not None else "").encode("utf-8", "replace").decode("utf-8")[:limit]


def clean(value, limit):
    return text(value or "", limit * 2).strip()[:limit]


def one_line(value, limit):
    """Questions become headings and names, so line breaks are folded."""
    return " ".join(text(value or "", limit * 2).split())[:limit]


def as_int(value):
    """An int from JSON, but not a bool, which Python counts as one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def clean_id(value):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isalnum() or ch in "-_.")[:64]


# Positions and sizes from the app, in scene units. Anything beyond this is a broken message.
MAX_COORDINATE = 1e7


def finite_floats(values, count, limit=MAX_COORDINATE):
    """count finite numbers within limit, or ValueError."""
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("wrong length")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("not a number")
        v = float(v)
        if not math.isfinite(v) or abs(v) > limit:
            raise ValueError("out of range")
        out.append(v)
    return out


# ---------------------------------------------------------------- colors


def to_srgb(c):
    c = min(max(float(c), 0.0), 1.0)
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def to_linear(c):
    c = min(max(float(c), 0.0), 1.0)
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# ---------------------------------------------------------------- matrices

IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
# Rhino (x, y, z) -> RealityKit (x, z, -y). Rhino is Z-up like Blender, so
# this is the Blender add-on's conversion: a turn of -90 degrees about X.
AXIS = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
AXIS_INV = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, -1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
# Undoes the app's -90 degree turn about local X for lights and cameras. The
# same numbers as AXIS_INV, named for what it does.
RX_90 = AXIS_INV


def mul(*matrices):
    """The product of 4x4 matrices, left to right."""
    out = matrices[0]
    for m in matrices[1:]:
        out = tuple(
            tuple(sum(out[r][k] * m[k][c] for k in range(4)) for c in range(4)) for r in range(4)
        )
    return out


def scaling(factor):
    return ((factor, 0.0, 0.0, 0.0), (0.0, factor, 0.0, 0.0), (0.0, 0.0, factor, 0.0), (0.0, 0.0, 0.0, 1.0))


def translation(x, y, z):
    return ((1.0, 0.0, 0.0, x), (0.0, 1.0, 0.0, y), (0.0, 0.0, 1.0, z), (0.0, 0.0, 0.0, 1.0))


def invert(m):
    """The inverse of a 4x4 matrix, or None when it has none."""
    a = [list(map(float, row)) + [1.0 if r == c else 0.0 for c in range(4)] for r, row in enumerate(m)]
    for col in range(4):
        pivot = max(range(col, 4), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(4):
            if r != col and a[r][col] != 0.0:
                f = a[r][col]
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return tuple(tuple(row[4:]) for row in a)


def flatten(m):
    """16 floats, column by column, as "transforms" carries them."""
    return [round(m[row][col], 6) + 0.0 for col in range(4) for row in range(4)]


def from_column_major(values):
    return tuple(tuple(float(values[col * 4 + row]) for col in range(4)) for row in range(4))


def matrix_from_message(values):
    """A column-major 4x4 matrix of 16 finite floats from the app, or None."""
    try:
        floats = finite_floats(values, 16)
    except ValueError:
        return None
    return from_column_major(floats)


def transform_point(m, p):
    return tuple(m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3] for r in range(3))


def column(m, index):
    return (m[0][index], m[1][index], m[2][index])


def is_similarity(m, tolerance=1e-6):
    """True for a rotation, uniform scale and translation, without mirroring
    or perspective. Only those keep a mesh's shape, so a move or rotation in
    Rhino can go to the app as a new matrix instead of a new mesh."""
    if any(abs(m[3][c] - (1.0 if c == 3 else 0.0)) > tolerance for c in range(4)):
        return False
    cols = [column(m, c) for c in range(3)]
    lengths = [sum(v * v for v in col) for col in cols]
    scale = lengths[0]
    if scale < 1e-12:
        return False
    for i in range(3):
        if abs(lengths[i] - scale) > tolerance * max(scale, 1.0) * 10:
            return False
        for j in range(i + 1, 3):
            dot = sum(a * b for a, b in zip(cols[i], cols[j]))
            if abs(dot) > tolerance * max(scale, 1.0) * 10:
                return False
    det = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )
    return det > 0


def frame_matrix(origin, x_axis, y_axis, z_axis):
    """A matrix whose columns are the given axes and origin."""
    return (
        (x_axis[0], y_axis[0], z_axis[0], origin[0]),
        (x_axis[1], y_axis[1], z_axis[1], origin[1]),
        (x_axis[2], y_axis[2], z_axis[2], origin[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def normalized(v):
    length = math.sqrt(sum(c * c for c in v))
    if length < 1e-12:
        return None
    return tuple(c / length for c in v)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def look_frame(origin, forward, up):
    """A Blender-style camera or light frame: local -Z along forward, local
    +Y as close to up as it can be. Returns None for a zero direction."""
    z = normalized(tuple(-c for c in forward))
    if z is None:
        return None
    up = normalized(up) or (0.0, 0.0, 1.0)
    x = normalized(cross(up, z))
    if x is None:  # up is parallel to the direction
        x = normalized(cross((0.0, 1.0, 0.0) if abs(z[1]) < 0.9 else (1.0, 0.0, 0.0), z))
    y = cross(z, x)
    return frame_matrix(origin, x, y, z)


# ---------------------------------------------------------------- meshes


@dataclass
class Geometry:
    """A triangulated mesh, ready for a kind 2 frame. blobs are positions,
    normals, UVs if any, triangle indices and per-triangle material indices,
    as little-endian bytes."""

    materials: list
    triangles: int = 0
    skipped: bool = False
    vertex_count: int = 0
    index_count: int = 0
    has_uvs: bool = False
    blobs: list = field(default_factory=list)


def encode_mesh(object_id, geometry, prototype=False):
    header = {
        "id": object_id,
        "vertexCount": geometry.vertex_count,
        "indexCount": geometry.index_count,
        "hasUVs": geometry.has_uvs,
        "materials": geometry.materials,
    }
    if prototype:
        header["prototype"] = True
    header_bytes = json.dumps(finite(header), separators=(",", ":")).encode("utf-8")
    payload = b"".join([struct.pack("<I", len(header_bytes)), header_bytes, *geometry.blobs])
    return encode_frame(KIND_MESH, payload)


def empty_geometry(materials, triangles=0, skipped=False):
    """No geometry to show: the app keeps the entity but removes its model."""
    return Geometry(materials=materials or [dict(DEFAULT_MATERIAL)], triangles=triangles, skipped=skipped)


# ---------------------------------------------------------------- names

_RESERVED = re.compile(r"[/#\x00-\x1f\x7f]+")


def readable_id(name, fallback="Object"):
    """A name as an object id. The app splits ids at "/" and "#" (instance
    ids are "parent/source#n") and keeps ids starting with "~" for itself."""
    value = _RESERVED.sub("-", str(name or ""))
    value = " ".join(value.split()).lstrip("~").strip()
    return value[:120] or fallback
