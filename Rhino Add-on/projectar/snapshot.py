# SPDX-License-Identifier: GPL-3.0-or-later
"""USDZ snapshots of the streamed scene, and sending them in chunks.

The Blender add-on uses Blender's USD exporter. Rhino's exporters differ
between versions and platforms, so the Rhino add-on writes the file itself
from the same meshes it streams: a USDA layer with UsdPreviewSurface
materials and the textures, Y up, packed into an uncompressed zip with every
file aligned to 64 bytes as USDZ requires. The app saves it to its Library.

It goes to the app in kind 4 frames, each at most CHUNK bytes of the file: a
little-endian uint32 header length, a JSON header {"id", "name", "format",
"index", "count", "size"}, then the bytes.
"""

import array
import io
import json
import re
import struct
import sys
import zipfile

from .protocol import key, to_linear
from .server import encode_frame

KIND_SNAPSHOT = 4
FEATURE = "snapshot"
CHUNK = 1024 * 1024
MAX_SIZE = 256 * 1024 * 1024


class SnapshotError(Exception):
    """An export that can't be sent. `reason` is the protocol's reason:
    tooLarge, exportFailed or nothingToExport."""

    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason


# ---------------------------------------------------------------- chunks


def chunk_count(size):
    return max(1, -(-size // CHUNK))


def chunk_frame(snapshot_id, name, data, index):
    """Kind 4 frame `index` of a snapshot."""
    count = chunk_count(len(data))
    header = json.dumps(
        {"id": snapshot_id, "name": name, "format": "usdz", "index": index, "count": count, "size": len(data)},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    body = data[index * CHUNK : (index + 1) * CHUNK]
    return encode_frame(KIND_SNAPSHOT, struct.pack("<I", len(header)) + header + body)


# ---------------------------------------------------------------- USDA


def _floats(blob):
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _uints(blob):
    values = array.array("I")
    if values.itemsize != 4:
        values = array.array("L")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _g(value):
    return "%.7g" % value


def _tuples(values, size):
    if size == 3:
        return ", ".join(
            f"({_g(values[i])}, {_g(values[i + 1])}, {_g(values[i + 2])})" for i in range(0, len(values) - 2, 3)
        )
    return ", ".join(f"({_g(values[i])}, {_g(values[i + 1])})" for i in range(0, len(values) - 1, 2))


def _ints(values):
    return ", ".join(str(int(v)) for v in values)


def _prim_name(name, taken):
    base = re.sub(r"[^A-Za-z0-9_]", "_", name or "Object").strip("_") or "Object"
    if base[0].isdigit():
        base = "_" + base
    base = base[:60]
    out, n = base, 2
    while out in taken:
        out = f"{base}_{n}"
        n += 1
    taken.add(out)
    return out


def _matrix(m):
    """16 column-major floats as a USD matrix4d. USD multiplies row vectors,
    so its rows are the columns."""
    rows = [m[i * 4 : i * 4 + 4] for i in range(4)]
    return "( " + ", ".join("(" + ", ".join(_g(v) for v in row) + ")" for row in rows) + " )"


def _texture_shader(lines, mat_path, name, file_name, color_space, output):
    lines += [
        f'            def Shader "{name}"',
        "            {",
        '                uniform token info:id = "UsdUVTexture"',
        f"                asset inputs:file = @{file_name}@",
        f"                float2 inputs:st.connect = <{mat_path}/PrimvarSt.outputs:result>",
        f'                token inputs:sourceColorSpace = "{color_space}"',
        '                token inputs:wrapS = "repeat"',
        '                token inputs:wrapT = "repeat"',
        f"                {output}",
        "            }",
    ]


def _material(lines, mat_path, prim, info, texture_files):
    color = list(info.get("color") or [0.8, 0.8, 0.8, 1.0]) + [1.0, 1.0, 1.0, 1.0]
    rgb = [to_linear(c) for c in color[:3]]
    alpha = min(max(float(color[3]), 0.0), 1.0)
    maps = info.get("textures") or {}
    files = {role: texture_files.get(tid) for role, tid in maps.items() if isinstance(tid, str)}
    files = {role: f for role, f in files.items() if f}
    inputs = []
    if files.get("baseColor"):
        inputs.append(f"                color3f inputs:diffuseColor.connect = <{mat_path}/BaseColor.outputs:rgb>")
        if maps.get("alpha"):
            inputs.append(f"                float inputs:opacity.connect = <{mat_path}/BaseColor.outputs:a>")
        else:
            inputs.append(f"                float inputs:opacity = {_g(alpha)}")
    else:
        inputs.append(f"                color3f inputs:diffuseColor = ({_g(rgb[0])}, {_g(rgb[1])}, {_g(rgb[2])})")
        inputs.append(f"                float inputs:opacity = {_g(alpha)}")
    for role, field in (("metallic", "metallic"), ("roughness", "roughness")):
        if files.get(role):
            inputs.append(f"                float inputs:{field}.connect = <{mat_path}/{role.title()}.outputs:r>")
        else:
            inputs.append(f"                float inputs:{field} = {_g(float(info.get(field, 0.5 if field == 'roughness' else 0.0)))}")
    if files.get("normal"):
        inputs.append(f"                normal3f inputs:normal.connect = <{mat_path}/Normal.outputs:rgb>")

    lines += [
        f'        def Material "{prim}"',
        "        {",
        f"            token outputs:surface.connect = <{mat_path}/Surface.outputs:surface>",
        '            def Shader "Surface"',
        "            {",
        '                uniform token info:id = "UsdPreviewSurface"',
        *inputs,
        "                int inputs:useSpecularWorkflow = 0",
        "                token outputs:surface",
        "            }",
    ]
    if files:
        lines += [
            '            def Shader "PrimvarSt"',
            "            {",
            '                uniform token info:id = "UsdPrimvarReader_float2"',
            '                string inputs:varname = "st"',
            "                float2 outputs:result",
            "            }",
        ]
        if files.get("baseColor"):
            _texture_shader(lines, mat_path, "BaseColor", files["baseColor"], "sRGB", "float3 outputs:rgb\n                float outputs:a")
        for role in ("metallic", "roughness"):
            if files.get(role):
                _texture_shader(lines, mat_path, role.title(), files[role], "raw", "float outputs:r")
        if files.get("normal"):
            lines += [
                '            def Shader "Normal"',
                "            {",
                '                uniform token info:id = "UsdUVTexture"',
                f"                asset inputs:file = @{files['normal']}@",
                f"                float2 inputs:st.connect = <{mat_path}/PrimvarSt.outputs:result>",
                '                token inputs:sourceColorSpace = "raw"',
                "                float4 inputs:scale = (2, 2, 2, 1)",
                "                float4 inputs:bias = (-1, -1, -1, 0)",
                "                float3 outputs:rgb",
                "            }",
            ]
    lines.append("        }")


def build_usda(items, texture_files, meters_per_unit):
    """The stage as text. items are (name, Geometry, 16 column-major floats)
    in RealityKit axes (Y up); texture_files maps texture ids to file names
    in the package."""
    lines = [
        "#usda 1.0",
        "(",
        '    defaultPrim = "Root"',
        f"    metersPerUnit = {_g(meters_per_unit)}",
        '    upAxis = "Y"',
        ")",
        "",
        'def Xform "Root"',
        "{",
    ]
    materials = {}  # key -> prim name
    material_lines = []
    mesh_lines = []
    taken_meshes, taken_materials = set(), set()

    def material_path(info):
        k = key(info)
        prim = materials.get(k)
        if prim is None:
            prim = materials[k] = _prim_name(info.get("name") or "Material", taken_materials)
            _material(material_lines, f"/Root/Materials/{prim}", prim, info, texture_files)
        return f"/Root/Materials/{prim}"

    for name, geometry, matrix in items:
        if geometry.skipped or not geometry.vertex_count or not geometry.index_count:
            continue
        blobs = geometry.blobs
        points = _floats(blobs[0])
        normals = _floats(blobs[1])
        uvs = _floats(blobs[2]) if geometry.has_uvs else None
        indices = _uints(blobs[-2])
        face_materials = _uints(blobs[-1])
        prim = _prim_name(name, taken_meshes)
        triangles = len(indices) // 3
        mesh_lines += [
            f'    def Mesh "{prim}" (',
            '        prepend apiSchemas = ["MaterialBindingAPI"]',
            "    )",
            "    {",
            "        uniform bool doubleSided = 0",
            f"        int[] faceVertexCounts = [{', '.join(['3'] * triangles)}]",
            f"        int[] faceVertexIndices = [{_ints(indices)}]",
            f"        normal3f[] normals = [{_tuples(normals, 3)}] (",
            '            interpolation = "vertex"',
            "        )",
            f"        point3f[] points = [{_tuples(points, 3)}]",
        ]
        if uvs is not None:
            mesh_lines += [
                f"        texCoord2f[] primvars:st = [{_tuples(uvs, 2)}] (",
                '            interpolation = "vertex"',
                "        )",
            ]
        mesh_lines += [
            '        uniform token subdivisionScheme = "none"',
            f"        matrix4d xformOp:transform = {_matrix(matrix)}",
            '        uniform token[] xformOpOrder = ["xformOp:transform"]',
        ]
        infos = geometry.materials or [{}]
        used = sorted(set(face_materials)) if len(infos) > 1 else [0]
        if len(used) <= 1:
            index = used[0] if used else 0
            mesh_lines.append(f"        rel material:binding = <{material_path(infos[min(index, len(infos) - 1)])}>")
        else:
            mesh_lines.append('        uniform token subsetFamily:materialBind:familyType = "partition"')
            for index in used:
                faces = [i for i, m in enumerate(face_materials) if m == index]
                mesh_lines += [
                    f'        def GeomSubset "Material_{index}" (',
                    '            prepend apiSchemas = ["MaterialBindingAPI"]',
                    "        )",
                    "        {",
                    '            uniform token elementType = "face"',
                    '            uniform token familyName = "materialBind"',
                    f"            int[] indices = [{_ints(faces)}]",
                    f"            rel material:binding = <{material_path(infos[min(index, len(infos) - 1)])}>",
                    "        }",
                ]
        mesh_lines.append("    }")

    if not taken_meshes:
        raise SnapshotError("nothingToExport")
    lines += ['    def Scope "Materials"', "    {", *material_lines, "    }", *mesh_lines, "}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- USDZ


def _package(files):
    """An uncompressed zip with each file's data aligned to 64 bytes. The
    first file must be the USD layer."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED, allowZip64=False) as zf:
        for name, data in files:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            offset = buffer.tell()
            header = 30 + len(name.encode("utf-8"))
            pad = (-(offset + header + 4)) % 64
            info.extra = struct.pack("<HH", 0x1986, pad) + b"\0" * pad
            zf.writestr(info, data)
    return buffer.getvalue()


def build_usdz(items, texture_data, meters_per_unit):
    """USDZ bytes for meshes in RealityKit axes. texture_data(tid) returns
    (format, bytes) or None. Raises SnapshotError."""
    files = []
    texture_files = {}
    for _, geometry, _ in items:
        for info in geometry.materials or []:
            for role, tid in (info.get("textures") or {}).items():
                if not isinstance(tid, str) or tid in texture_files:
                    continue
                result = texture_data(tid)
                if not result:
                    continue
                fmt, data = result
                name = f"textures/{re.sub(r'[^A-Za-z0-9]', '', tid)}.{'jpg' if fmt == 'jpeg' else 'png'}"
                texture_files[tid] = name
                files.append((name, data))
    stage = build_usda(items, texture_files, meters_per_unit).encode("utf-8")
    if len(stage) + sum(len(d) for _, d in files) > MAX_SIZE:
        raise SnapshotError("tooLarge", f"{len(stage)} bytes of geometry")
    try:
        data = _package([("scene.usda", stage)] + files)
    except (zipfile.LargeZipFile, OverflowError) as exc:
        raise SnapshotError("tooLarge", str(exc)) from exc
    if len(data) > MAX_SIZE:
        raise SnapshotError("tooLarge", f"{len(data)} bytes")
    return data
