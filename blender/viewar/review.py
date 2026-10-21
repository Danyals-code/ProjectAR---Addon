# SPDX-License-Identifier: GPL-3.0-or-later
"""Design review data shared with the app: variant sets and notes.

A variant set is a collection marked "AR Variant Set". Each child collection
is one design option, and the visible one is the active option.

Notes are empties in the "ProjectAR Notes" collection that carry their text in
custom properties. The app places them on the model, and they can also be
added, edited or deleted in Blender.
"""

import datetime
import uuid

import bpy
from mathutils import Matrix, Vector

# Blender (x, y, z) -> RealityKit (x, z, -y), as in scene_stream.
AXIS = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))

NOTES_COLLECTION = "ProjectAR Notes"
# Files saved before the add-on was renamed keep their notes here.
LEGACY_NOTES_COLLECTION = "ViewAR Notes"
MAX_NOTE_LENGTH = 1000
NOTE_SIZE_METERS = 0.05


# ---------------------------------------------------------------- variants


def layer_collections(view_layer):
    """Collection name -> LayerCollection for the whole view layer tree."""
    out = {}
    stack = [view_layer.layer_collection]
    while stack:
        layer = stack.pop()
        out[layer.collection.name] = layer
        stack.extend(layer.children)
    return out


def _is_shown(layer):
    return (
        layer is not None
        and not layer.exclude
        and not layer.hide_viewport
        and not layer.collection.hide_viewport
    )


def variant_set_collections(scene):
    return [
        c
        for c in scene.collection.children_recursive
        if getattr(c, "viewar_variant_set", False) and len(c.children) > 0
    ]


def variant_sets(scene, view_layer):
    """Variant sets as sent to the app, in outliner order."""
    layers = layer_collections(view_layer)
    sets = []
    for coll in variant_set_collections(scene):
        options = list(coll.children)
        active = next((c.name for c in options if _is_shown(layers.get(c.name))), options[0].name)
        sets.append(
            {
                "name": coll.name,
                "active": active,
                "options": [
                    {"name": c.name, "objects": sorted(o.name_full for o in c.all_objects)}
                    for c in options
                ],
            }
        )
    return sets


def variant_object_names(sets):
    return {name for s in sets for option in s["options"] for name in option["objects"]}


def apply_variant(view_layer, set_name, option_name):
    """Shows one option of a variant set and hides the others. Returns False
    if the set or option doesn't exist."""
    coll = bpy.data.collections.get(set_name)
    if coll is None or not getattr(coll, "viewar_variant_set", False):
        return False
    if option_name not in coll.children:
        return False
    layers = layer_collections(view_layer)
    for child in coll.children:
        layer = layers.get(child.name)
        if layer is None:
            continue
        show = child.name == option_name
        if show:
            layer.exclude = False
        layer.hide_viewport = not show
    return True


# ---------------------------------------------------------------- notes


def _existing_notes_collection():
    return bpy.data.collections.get(NOTES_COLLECTION) or bpy.data.collections.get(
        LEGACY_NOTES_COLLECTION
    )


def note_objects(scene):
    coll = _existing_notes_collection()
    if coll is None:
        return []
    names = {o.name_full for o in scene.objects}
    return [o for o in coll.all_objects if o.get("viewar_note_id") and o.name_full in names]


def find_note(scene, note_id):
    return next((o for o in note_objects(scene) if o.get("viewar_note_id") == note_id), None)


def notes(scene, space):
    """Notes as sent to the app. Positions are in the same space as transforms."""
    out = []
    for obj in note_objects(scene):
        p = space @ obj.matrix_world.translation
        note = {
            "id": str(obj["viewar_note_id"]),
            "text": str(obj.get("viewar_note_text", "")),
            "author": str(obj.get("viewar_note_author", "")),
            "created": str(obj.get("viewar_note_created", "")),
            "position": [round(p.x, 5), round(p.y, 5), round(p.z, 5)],
        }
        target = obj.get("viewar_note_object")
        if target:
            note["object"] = str(target)
        out.append(note)
    out.sort(key=lambda n: (n["created"], n["id"]))
    return out


def _notes_collection(scene):
    coll = _existing_notes_collection()
    if coll is None:
        coll = bpy.data.collections.new(NOTES_COLLECTION)
    if coll not in scene.collection.children_recursive:
        scene.collection.children.link(coll)
    return coll


def _note_name(text):
    first_line = text.strip().splitlines()[0] if text.strip() else "Note"
    name = f"Note: {first_line}"
    # Blender names are limited to 63 bytes.
    while len(name.encode("utf-8")) > 63:
        name = name[:-1]
    return name


def add_note(scene, location, text, author, note_id=None, target=None, created=None):
    """Creates a note empty at a Blender world location and returns it."""
    text = str(text)[:MAX_NOTE_LENGTH]
    empty = bpy.data.objects.new(_note_name(text), None)
    unit = scene.unit_settings.scale_length or 1.0
    empty.empty_display_type = "SPHERE"
    empty.empty_display_size = NOTE_SIZE_METERS / unit
    empty.show_name = True
    empty.show_in_front = True
    empty.location = location
    empty["viewar_note_id"] = note_id or uuid.uuid4().hex
    empty["viewar_note_text"] = text
    empty["viewar_note_author"] = str(author)[:80]
    empty["viewar_note_created"] = created or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if target:
        empty["viewar_note_object"] = str(target)[:200]
    _notes_collection(scene).objects.link(empty)
    return empty


def remove_note(scene, note_id):
    obj = find_note(scene, note_id)
    if obj is None:
        return False
    bpy.data.objects.remove(obj, do_unlink=True)
    return True


# ---------------------------------------------------------------- room scans

SCAN_COLLECTION = "Room Scan"
# Kind -> (sub-collection, material name, sRGB-ish color with alpha)
SCAN_KINDS = {
    "wall": ("Walls", "ProjectAR Scan Wall", (0.85, 0.85, 0.83, 1.0)),
    "floor": ("Floors", "ProjectAR Scan Floor", (0.55, 0.55, 0.55, 1.0)),
    "door": ("Openings", "ProjectAR Scan Door", (0.55, 0.42, 0.3, 1.0)),
    "window": ("Openings", "ProjectAR Scan Window", (0.6, 0.78, 0.9, 0.35)),
    "opening": ("Openings", "ProjectAR Scan Opening", (0.9, 0.9, 0.9, 0.2)),
    "object": ("Furniture", "ProjectAR Scan Object", (0.7, 0.7, 0.72, 1.0)),
}
MAX_SCAN_ITEMS = 2000


def _scan_material(name, color):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = color
        tree = mat.node_tree
        principled = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if tree else None
        if principled is not None:
            principled.inputs["Base Color"].default_value = color
            principled.inputs["Alpha"].default_value = color[3]
        if color[3] < 1.0:
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
            elif hasattr(mat, "blend_method"):
                mat.blend_method = "BLEND"
    return mat


def _box_mesh(name, size):
    """A box centered on the origin, size = (x, y, z) in Blender axes."""
    x, y, z = (max(float(v), 1e-4) / 2 for v in size)
    verts = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
             (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    return mesh


def _polygon_mesh(name, corners):
    """A flat face from RealityKit-local corners, converted to Blender axes."""
    verts = [(c[0], -c[2], c[1]) for c in corners]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], [tuple(range(len(verts)))])
    mesh.validate()
    return mesh


def _matrix(values):
    """Column-major 16 floats -> mathutils Matrix."""
    return Matrix([[values[col * 4 + row] for col in range(4)] for row in range(4)])


def add_room_scan(scene, message, space):
    """Builds a room scan from the app as named boxes in a new collection.

    Each item has a kind, a RealityKit-space transform (rotation and
    translation in scene units) and dimensions (width, height, depth) in
    scene units. Floors may carry polygon corners in their local space.
    Scan objects are kept out of AR, since the real room is already there.
    """
    items = message.get("items")
    if not isinstance(items, list) or not items:
        return None
    root = bpy.data.collections.new(str(message.get("name") or SCAN_COLLECTION)[:60])
    scene.collection.children.link(root)
    root["viewar_scan_id"] = str(message.get("id", ""))[:64]
    root["viewar_scan_aligned"] = bool(message.get("aligned"))
    subs = {}
    counts = {}
    space_inv = space.inverted_safe()
    for item in items[:MAX_SCAN_ITEMS]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in SCAN_KINDS:
            continue
        transform = item.get("transform")
        dims = item.get("dimensions")
        if not (isinstance(transform, list) and len(transform) == 16 and isinstance(dims, list) and len(dims) == 3):
            continue
        try:
            m_rk = _matrix([float(v) for v in transform])
            width, height, depth = (float(v) for v in dims)
        except (TypeError, ValueError):
            continue
        sub_name, mat_name, color = SCAN_KINDS[kind]
        label = str(item.get("category") or kind).replace("_", " ").title() if kind == "object" else kind.title()
        counts[label] = counts.get(label, 0) + 1
        name = label if kind == "floor" and counts[label] == 1 else f"{label} {counts[label]}"

        corners = item.get("polygon")
        if kind == "floor" and isinstance(corners, list) and len(corners) >= 3:
            try:
                mesh = _polygon_mesh(name, [[float(v) for v in c][:3] for c in corners])
            except (TypeError, ValueError, IndexError):
                mesh = _box_mesh(name, (width, depth, height))
        else:
            # RealityKit local (width x, height y, depth z) is Blender (x, depth y, height z).
            mesh = _box_mesh(name, (width, depth, height))
        mesh.materials.append(_scan_material(mat_name, color))
        obj = bpy.data.objects.new(name, mesh)
        obj.matrix_world = space_inv @ m_rk @ AXIS
        obj.viewar_visible = False
        obj["viewar_scan_kind"] = kind
        if kind == "object":
            obj["viewar_scan_category"] = str(item.get("category", ""))[:40]

        sub = subs.get(sub_name)
        if sub is None:
            sub = subs[sub_name] = bpy.data.collections.new(f"{sub_name}")
            root.children.link(sub)
        sub.objects.link(obj)
    return root


# ---------------------------------------------------------------- messages


def _find_object(name):
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.name_full == name:
        return obj
    return next((o for o in bpy.data.objects if o.name_full == name), None)


def handle_message(message, scene, view_layer, space):
    """Applies one message from the app. Unknown types are ignored, so newer
    apps work with this add-on. Returns True if the scene was changed."""
    kind = message.get("type")
    if kind == "setMaterial":
        obj = _find_object(str(message.get("object", "")))
        mat = bpy.data.materials.get(str(message.get("material", "")))
        slot = message.get("slot")
        if obj is None or mat is None or not isinstance(slot, int):
            return False
        if not 0 <= slot < len(obj.material_slots) or obj.library is not None:
            return False
        obj.material_slots[slot].material = mat
        return True
    if kind == "setVariant":
        return apply_variant(view_layer, str(message.get("set", "")), str(message.get("option", "")))
    if kind == "addNote":
        note_id = str(message.get("id", ""))[:64]
        position = message.get("position")
        if not note_id or find_note(scene, note_id) is not None:
            return False
        if not (isinstance(position, list) and len(position) == 3):
            return False
        try:
            point = Vector([float(v) for v in position])
        except (TypeError, ValueError):
            return False
        location = space.inverted_safe() @ point
        add_note(
            scene,
            location,
            message.get("text", ""),
            message.get("author", ""),
            note_id=note_id,
            target=message.get("object"),
            created=str(message.get("created") or "")[:40] or None,
        )
        return True
    if kind == "removeNote":
        return remove_note(scene, str(message.get("id", "")))
    if kind == "addRoomScan":
        return add_room_scan(scene, message, space) is not None
    return False
