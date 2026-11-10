# SPDX-License-Identifier: GPL-3.0-or-later
"""USDZ snapshots of the streamed scene, for full material fidelity.

Blender's USD exporter writes what is streamed (scope, visibility and Show in
AR) to a temporary .usdz file. It goes to the app in kind 4 frames, each at
most CHUNK bytes of the file: a little-endian uint32 header length, a JSON
header {"id", "name", "format", "index", "count", "size"}, then the bytes.
"""

import json
import os
import shutil
import struct
import tempfile

import bpy

from .server import encode_frame

KIND_SNAPSHOT = 4
FEATURE = "snapshot"
CHUNK = 1024 * 1024
MAX_SIZE = 256 * 1024 * 1024
# Object types the exporter turns into geometry. Empties count when they instance a collection.
_GEOMETRY_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD"}


class SnapshotError(Exception):
    """An export that can't be sent. `reason` is the protocol's reason."""

    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason


def _visible(obj, view_layer):
    try:
        return obj.visible_get(view_layer=view_layer)
    except RuntimeError:
        return False


def exported_objects(view_layer, accept):
    """What a snapshot contains: visible, in scope, shown in AR, and either
    geometry or an empty that instances a collection."""
    out = []
    for obj in view_layer.objects:
        if not accept(obj) or not _visible(obj, view_layer):
            continue
        if obj.type in _GEOMETRY_TYPES or (obj.type == "EMPTY" and obj.instance_type == "COLLECTION" and obj.instance_collection):
            out.append(obj)
    return out


def _export_options(unit_scale):
    """Keyword arguments for wm.usd_export, keeping only the ones this Blender has.

    The exporter doesn't scale geometry, so a Unit Scale other than 1 goes
    into the stage's metersPerUnit instead."""
    custom_units = abs(unit_scale - 1.0) > 1e-9
    wanted = {
        "selected_objects_only": True,
        "export_materials": True,
        "generate_preview_surface": True,
        "generate_materialx_network": False,
        "export_textures_mode": "NEW",
        "export_textures": True,  # before export_textures_mode
        "overwrite_textures": True,
        "convert_orientation": True,
        "export_global_forward_selection": "NEGATIVE_Z",
        "export_global_up_selection": "Y",
        "convert_scene_units": "CUSTOM" if custom_units else "METERS",
        "meters_per_unit": unit_scale,
        "evaluation_mode": "VIEWPORT",
        # Realized geometry: RealityKit doesn't read every kind of USD instancing.
        "use_instancing": False,
        "export_lights": False,
        "export_cameras": False,
        "convert_world_material": False,
        "export_animation": False,
        "usdz_downscale_size": "KEEP",
    }
    if not custom_units:
        del wanted["meters_per_unit"]
    available = {p.identifier for p in bpy.ops.wm.usd_export.get_rna_type().properties}
    return {k: v for k, v in wanted.items() if k in available}


def _run_export(path, unit_scale):
    options = _export_options(unit_scale)
    try:
        result = bpy.ops.wm.usd_export(filepath=path, **options)
    except RuntimeError:
        # From a timer the operator may need a window to run in.
        windows = bpy.context.window_manager.windows
        if not windows:
            raise
        with bpy.context.temp_override(window=windows[0]):
            result = bpy.ops.wm.usd_export(filepath=path, **options)
    if "FINISHED" not in result:
        raise SnapshotError("exportFailed", f"exporter returned {result}")


def export_usdz(scene, view_layer, accept):
    """The streamed scene as USDZ bytes. Raises SnapshotError."""
    targets = exported_objects(view_layer, accept)
    if not targets:
        raise SnapshotError("nothingToExport")
    selected = [o for o in view_layer.objects if o.select_get(view_layer=view_layer)]
    was_selected = {o.as_pointer() for o in selected}
    active = view_layer.objects.active
    folder = tempfile.mkdtemp(prefix="projectar-snapshot-")
    path = os.path.join(folder, "snapshot.usdz")
    try:
        for obj in selected:
            obj.select_set(False, view_layer=view_layer)
        for obj in targets:
            try:
                obj.select_set(True, view_layer=view_layer)
            except RuntimeError:
                pass  # can't be selected here, so it's left out
        try:
            _run_export(path, float(scene.unit_settings.scale_length) or 1.0)
        except SnapshotError:
            raise
        except Exception as exc:
            raise SnapshotError("exportFailed", str(exc)) from exc
        if not os.path.isfile(path):
            raise SnapshotError("exportFailed", "no file written")
        size = os.path.getsize(path)
        if size > MAX_SIZE:
            raise SnapshotError("tooLarge", f"{size} bytes")
        with open(path, "rb") as f:
            return f.read()
    finally:
        for obj in view_layer.objects:
            try:
                obj.select_set(obj.as_pointer() in was_selected, view_layer=view_layer)
            except RuntimeError:
                pass
        try:
            view_layer.objects.active = active
        except (RuntimeError, ReferenceError):
            pass
        shutil.rmtree(folder, ignore_errors=True)


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
