# SPDX-License-Identifier: GPL-3.0-or-later
"""Design review data shared with the app: variant sets, notes and decisions.

A variant set is a collection marked "AR Variant Set". Each child collection
is one design option, and the visible one is the active option.

Notes are empties in the "ProjectAR Notes" collection that carry their text in
custom properties. The app places them on the model, and they can also be
added, edited or deleted in Blender.

Decisions are made in the app or in Blender and kept in the scene, in
question, options and criteria (QOC) form: what was decided, the options
compared, the one chosen, what mattered, and why. Each one's photo is packed
into the file as an image, and the "ProjectAR Decisions" text lists them all
for reading.
"""

import base64
import binascii
import datetime
import json
import math
import os
import re
import tempfile
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


def shown_variant_sets(sets, view_layer):
    """Variant sets whose own collection, and every collection above it, is shown."""
    shown = set()
    stack = [(view_layer.layer_collection, True)]
    while stack:
        layer, parent_shown = stack.pop()
        here = parent_shown and (layer == view_layer.layer_collection or _is_shown(layer))
        if here:
            shown.add(layer.collection.name)
        stack.extend((child, here) for child in layer.children)
    return [s for s in sets if s["name"] in shown]


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
    shown = [c.name for c in coll.children if _is_shown(layers.get(c.name))]
    if shown == [option_name]:
        return False  # already showing, nothing to change
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


# Each scene has its own notes collection, found by this tag so renaming it is fine.
NOTES_TAG = "viewar_notes"
MAX_NOTES = 1000


def _existing_notes_collection(scene):
    children = scene.collection.children_recursive
    tagged = next((c for c in children if c.get(NOTES_TAG)), None)
    if tagged is not None:
        return tagged
    # Files from before the tag: the collection is known by its name. It's
    # tagged when a note is next added, since panels can't change data.
    return next((c for c in children if c.name in (NOTES_COLLECTION, LEGACY_NOTES_COLLECTION)), None)


def note_id_of(obj):
    """A note's id as text, or "" if it has none. Hand edits can leave other types."""
    value = obj.get("viewar_note_id")
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value)[:64]
    return ""


def note_objects(scene):
    coll = _existing_notes_collection(scene)
    if coll is None:
        return []
    names = {o.name_full for o in scene.objects}
    return [o for o in coll.all_objects if note_id_of(o) and o.name_full in names]


def find_note(scene, wanted):
    wanted = str(wanted)
    return next((o for o in note_objects(scene) if note_id_of(o) == wanted), None)


def notes(scene, space):
    """Notes as sent to the app. Positions are in the same space as transforms."""
    out = []
    for obj in note_objects(scene):
        p = space @ obj.matrix_world.translation
        note = {
            "id": note_id_of(obj),
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
    coll = _existing_notes_collection(scene)
    if coll is None:
        # Not shared with other scenes, so their notes stay apart.
        coll = bpy.data.collections.new(NOTES_COLLECTION)
        scene.collection.children.link(coll)
    if not coll.get(NOTES_TAG) and coll.library is None:
        coll[NOTES_TAG] = True
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


# ---------------------------------------------------------------- decisions

DECISIONS_PROPERTY = "viewar_decisions"
DECISIONS_TEXT = "ProjectAR Decisions"
DECISION_KINDS = ("variant", "material", "custom")
# Where a decision was made: in the room with the app, or at the screen in Blender.
DECISION_SOURCES = ("app", "blender")
# The app's suggestions, so both places offer the same criteria.
SUGGESTED_CRITERIA = (
    "Fits the space",
    "Size and proportion",
    "Looks right in this light",
    "Material and finish",
    "Room to move around it",
    "Goes with the room",
    "Cost",
)
MAX_DECISION_TEXT = 2000
MAX_DECISION_OPTIONS = 50
# Base64 of the photo. The app sends JPEGs of about 300 KB.
MAX_PHOTO_CHARS = 3 * 1024 * 1024


def decisions(scene):
    """The scene's decisions, oldest first."""
    raw = scene.get(DECISIONS_PROPERTY)
    if not raw:
        return []
    try:
        items = json.loads(str(raw))
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    # The property can be edited by hand, so only well-formed records count.
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        if not all(isinstance(d.get(k), str) and d.get(k) for k in ("id", "question", "chosen")):
            continue
        for key in ("options", "criteria"):
            if not isinstance(d.get(key), list):
                d[key] = []
            d[key] = [str(v) for v in d[key]]
        for key in ("reason", "author", "created"):
            if not isinstance(d.get(key), str):
                d[key] = ""
        for key in ("photo", "set", "object", "kind"):
            if key in d and not isinstance(d[key], str):
                del d[key]
        if "slot" in d and _int(d["slot"]) is None:
            del d["slot"]
        if "confidence" in d and not (_int(d["confidence"]) is not None and 1 <= d["confidence"] <= 5):
            del d["confidence"]
        # Before Blender could record decisions, all came from the app.
        if d.get("source") not in DECISION_SOURCES:
            d["source"] = "app"
        out.append(d)
    return out


def find_decision(scene, decision_id):
    return next((d for d in decisions(scene) if d["id"] == decision_id), None)


def decision_image(decision):
    name = decision.get("photo")
    image = bpy.data.images.get(name) if name else None
    if image is not None and image.get("viewar_decision_id") == decision["id"]:
        return image
    return next((i for i in bpy.data.images if i.get("viewar_decision_id") == decision["id"]), None)


def _store_decisions(scene, items):
    scene[DECISIONS_PROPERTY] = json.dumps(items, ensure_ascii=False)
    _write_decisions_text(scene, items)


def text(value, limit):
    """Text from the app, made safe to store: lone surrogates in JSON become
    replacement characters instead of errors when the file is saved."""
    return str(value if value is not None else "").encode("utf-8", "replace").decode("utf-8")[:limit]


def _clean(value, limit):
    return text(value or "", limit * 2).strip()[:limit]


def _one_line(value, limit):
    """Questions become headings and image names, so line breaks are folded."""
    return " ".join(text(value or "", limit * 2).split())[:limit]


def _int(value):
    """An int from JSON, but not a bool, which Python counts as one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Positions and sizes from the app, in meters. Anything beyond this is a broken message.
MAX_COORDINATE = 1e5


def finite_floats(values, count):
    """count finite numbers within MAX_COORDINATE, or ValueError."""
    if not isinstance(values, list) or len(values) != count:
        raise ValueError("wrong length")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("not a number")
        v = float(v)
        if not math.isfinite(v) or abs(v) > MAX_COORDINATE:
            raise ValueError("out of range")
        out.append(v)
    return out


def safe_file_id(decision_id):
    """A decision id as it may appear in a file name."""
    return re.sub(r"[^A-Za-z0-9-]", "", decision_id)[:36] or "decision"


def _image_name(question):
    # Blender adds .001 and so on when names repeat.
    name = f"Decision: {question}" if question else "Decision"
    while len(name.encode("utf-8")) > 63:
        name = name[:-1]
    return name


def _pack_photo(data, name, decision_id):
    """Packs JPEG bytes into the file as an image. Returns its name, or None."""
    handle, path = tempfile.mkstemp(suffix=".jpg")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
        image = bpy.data.images.load(path)
        image.pack()
        # Unpacking writes the photo next to the .blend file.
        image.filepath_raw = f"//ProjectAR Decisions/{safe_file_id(decision_id)[:8]}.jpg"
        image.name = name
        image["viewar_decision_id"] = decision_id
        # Nothing uses the image, so keep it when the file is saved.
        image.use_fake_user = True
        return image.name
    except RuntimeError as exc:  # not a readable image
        print(f"ProjectAR: could not keep the photo of a decision: {exc}")
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def add_decision(scene, message, source="app"):
    """Keeps a decision from the app, or one recorded in Blender when source
    is "blender". Returns it, or None if it's not valid or already kept."""
    decision_id = _clean(message.get("id"), 64)
    question = _one_line(message.get("question"), MAX_DECISION_TEXT)
    chosen = _one_line(message.get("chosen"), 200)
    options = message.get("options")
    if not decision_id or not question or not chosen or not isinstance(options, list):
        return None
    if find_decision(scene, decision_id) is not None:
        return None
    options = [_one_line(o, 200) for o in options[:MAX_DECISION_OPTIONS] if _one_line(o, 200)]
    options = list(dict.fromkeys(options))  # no repeats
    if chosen not in options:
        options.append(chosen)
    criteria = message.get("criteria")
    criteria = [_clean(c, 200) for c in criteria[:MAX_DECISION_OPTIONS]] if isinstance(criteria, list) else []
    kind = message.get("kind") if message.get("kind") in DECISION_KINDS else "custom"
    confidence = _int(message.get("confidence"))
    if not (confidence is not None and 1 <= confidence <= 5):
        confidence = None

    items = decisions(scene)
    decision = {
        "id": decision_id,
        "created": _clean(message.get("created"), 40)
        or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "author": _clean(message.get("author"), 80),
        "question": question,
        "kind": kind,
        "options": options,
        "chosen": chosen,
        "criteria": [c for c in criteria if c],
        "reason": _clean(message.get("reason"), MAX_DECISION_TEXT),
        "source": source if source in DECISION_SOURCES else "app",
    }
    if kind == "variant" and message.get("set"):
        decision["set"] = _clean(message.get("set"), 200)
    if kind == "material" and message.get("object"):
        decision["object"] = _clean(message.get("object"), 200)
        if _int(message.get("slot")) is not None:
            decision["slot"] = message["slot"]
    if confidence is not None:
        decision["confidence"] = confidence

    photo = message.get("photo")
    if isinstance(photo, str) and 0 < len(photo) <= MAX_PHOTO_CHARS:
        try:
            data = base64.b64decode(photo, validate=True)
        except (binascii.Error, ValueError):
            data = None
        if data:
            name = _pack_photo(data, _image_name(question), decision_id)
            if name:
                decision["photo"] = name

    items.append(decision)
    _store_decisions(scene, items)
    return decision


def record_decision(scene, view_layer, question, options, chosen, reason, *,
                    set_name="", criteria=(), confidence=None, author="Blender", photo=None):
    """Records a decision made in Blender, stored exactly like one from the
    app. For a variant set, the chosen option is shown. photo is JPEG bytes.
    Returns the decision, or None if it's not valid."""
    message = {
        "id": str(uuid.uuid4()).upper(),  # like the app's ids
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "author": author or "Blender",
        "question": question,
        "kind": "variant" if set_name else "custom",
        "options": list(options),
        "chosen": chosen,
        "criteria": list(criteria),
        "reason": reason,
        "confidence": confidence,
    }
    if set_name:
        message["set"] = set_name
    if photo:
        message["photo"] = base64.b64encode(photo).decode("ascii")
    decision = add_decision(scene, message, source="blender")
    if decision is not None and set_name:
        apply_variant(view_layer, set_name, decision["chosen"])
    return decision


def remove_decision(scene, decision_id):
    items = decisions(scene)
    decision = next((d for d in items if d["id"] == decision_id), None)
    if decision is None:
        return False
    image = decision_image(decision)
    if image is not None:
        bpy.data.images.remove(image)
    _store_decisions(scene, [d for d in items if d["id"] != decision_id])
    return True


DECISIONS_TEXT_PROPERTY = "viewar_decisions_text"


def decisions_text(scene):
    """The scene's log text, if it has one. Each scene has its own."""
    name = scene.get(DECISIONS_TEXT_PROPERTY)
    log = bpy.data.texts.get(name) if isinstance(name, str) else None
    if log is not None:
        # A copied scene points at the original's text until it writes its own.
        owner = bpy.data.scenes.get(log.get("viewar_scene", ""))
        if owner is None or owner == scene or owner.get(DECISIONS_TEXT_PROPERTY) != log.name:
            return log
    # Files from before each scene had its own: the shared text, if no other scene claimed it.
    log = bpy.data.texts.get(DECISIONS_TEXT)
    claimed = any(s.get(DECISIONS_TEXT_PROPERTY) == DECISIONS_TEXT for s in bpy.data.scenes if s != scene)
    if log is not None and not claimed and log.get("viewar_scene") in (None, scene.name):
        return log
    return None


def _write_decisions_text(scene, items):
    log = decisions_text(scene)
    if log is None:
        if not items:
            return
        # The first scene keeps the plain name, others add theirs.
        name = DECISIONS_TEXT if bpy.data.texts.get(DECISIONS_TEXT) is None else f"{DECISIONS_TEXT}: {scene.name}"
        while len(name.encode("utf-8")) > 63:
            name = name[:-1]
        log = bpy.data.texts.new(name)
    log["viewar_scene"] = scene.name
    scene[DECISIONS_TEXT_PROPERTY] = log.name
    log.clear()
    log.write(decisions_markdown(scene, items))


def decisions_markdown(scene, items):
    """The log as Markdown, one QOC entry per decision, oldest first."""
    lines = [
        f"# Decisions for {scene.name}",
        "",
        "Made in the room with ProjectAR or at the screen in Blender. Each one lists",
        "the question, the options compared, the one chosen, the criteria that",
        "mattered and the reason.",
        "This text is rewritten when decisions change, so edits here are not kept.",
    ]
    for number, d in enumerate(items, start=1):
        lines += ["", f"## {number}. {d['question']}", ""]
        lines.append(f"- Options: {', '.join(d.get('options', []))}")
        lines.append(f"- Chosen: {d.get('chosen', '')}")
        if d.get("set"):
            lines.append(f"- Variant set: {d['set']}")
        if d.get("object"):
            slot = d.get("slot")
            where = f"{d['object']}, slot {slot + 1}" if isinstance(slot, int) else d["object"]
            lines.append(f"- Material slot: {where}")
        if d.get("criteria"):
            lines.append(f"- Criteria: {', '.join(d['criteria'])}")
        lines.append(f"- Reason: {d.get('reason', '')}")
        if d.get("confidence"):
            lines.append(f"- Confidence: {d['confidence']} of 5")
        if d.get("photo"):
            lines.append(f"- Photo: image \"{d['photo']}\"")
        where = "in Blender" if d.get("source") == "blender" else "in the room"
        lines.append(f"- Made {where}")
        by = d.get("author") or "unknown"
        lines.append(f"- By {by}, {d.get('created', '')}")
    return "\n".join(lines) + "\n"


CSV_COLUMNS = (
    "id", "created", "scene", "author", "question", "kind", "variant_set", "object", "slot",
    "options", "chosen", "criteria", "reason", "confidence", "photo", "source",
)


def _csv_field(value):
    value = str(value)
    if any(c in value for c in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def photo_file_name(decision):
    """As the app names photos in its export."""
    return f"decision-{safe_file_id(decision['id'])[:8].lower()}.jpg"


def decisions_csv(scene, items):
    """The log as CSV, with the app's export columns and then the source,
    so the two can be put together."""
    rows = [",".join(CSV_COLUMNS)]
    for d in items:
        slot = d.get("slot")
        rows.append(",".join(_csv_field(v) for v in (
            d["id"],
            d.get("created", ""),
            scene.name,
            d.get("author", ""),
            d["question"],
            d.get("kind", "custom"),
            d.get("set", ""),
            d.get("object", ""),
            slot if isinstance(slot, int) else "",
            " | ".join(d.get("options", [])),
            d["chosen"],
            " | ".join(d.get("criteria", [])),
            d.get("reason", ""),
            d.get("confidence") or "",
            photo_file_name(d) if decision_image(d) is not None else "",
            d.get("source", "app"),
        )))
    return "\n".join(rows) + "\n"


def export_decisions(scene, csv_path):
    """Writes the CSV and, in a folder beside it, each decision's photo.
    Returns the number of decisions and of photos written."""
    items = decisions(scene)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(decisions_csv(scene, items))
    folder = os.path.splitext(csv_path)[0] + " Photos"
    photos = 0
    for d in items:
        image = decision_image(d)
        if image is None or image.packed_file is None:
            continue
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, photo_file_name(d)), "wb") as f:
            f.write(bytes(image.packed_file.data))
        photos += 1
    return len(items), photos


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
# A room has tens of items. This bounds how long one scan takes to build.
MAX_SCAN_ITEMS = 500
MAX_SCAN_SIZE = 1000.0


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
    # Nothing is created until an item turns out to be valid.
    root = None
    subs = {}
    counts = {}
    space_inv = space.inverted_safe()
    for item in items[:MAX_SCAN_ITEMS]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in SCAN_KINDS:
            continue
        transform = item.get("transform")
        dims = item.get("dimensions")
        if not (isinstance(transform, list) and len(transform) == 16 and isinstance(dims, list) and len(dims) == 3):
            continue
        try:
            m_rk = _matrix(finite_floats(transform, 16))
            width, height, depth = (min(abs(v), MAX_SCAN_SIZE) for v in finite_floats(dims, 3))
        except ValueError:
            continue
        if root is None:
            root = bpy.data.collections.new(_one_line(message.get("name") or SCAN_COLLECTION, 60) or SCAN_COLLECTION)
            scene.collection.children.link(root)
            root["viewar_scan_id"] = _clean(message.get("id", ""), 64)
            root["viewar_scan_aligned"] = message.get("aligned") is True
        sub_name, mat_name, color = SCAN_KINDS[kind]
        label = _one_line(item.get("category") or kind, 40).replace("_", " ").title() if kind == "object" else kind.title()
        counts[label] = counts.get(label, 0) + 1
        name = label if kind == "floor" and counts[label] == 1 else f"{label} {counts[label]}"

        corners = item.get("polygon")
        if kind == "floor" and isinstance(corners, list) and len(corners) >= 3:
            try:
                mesh = _polygon_mesh(name, [finite_floats(c[:3] if isinstance(c, list) else c, 3) for c in corners[:256]])
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
            obj["viewar_scan_category"] = _one_line(item.get("category", ""), 40)

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
        slot = _int(message.get("slot"))
        if obj is None or mat is None or slot is None:
            return False
        if not 0 <= slot < len(obj.material_slots) or obj.library is not None:
            return False
        # A slot linked to the mesh can't change when the mesh comes from a library.
        if obj.material_slots[slot].link == "DATA" and obj.data is not None and obj.data.library is not None:
            return False
        if obj.material_slots[slot].material == mat:
            return False
        obj.material_slots[slot].material = mat
        return True
    if kind == "setVariant":
        return apply_variant(view_layer, text(message.get("set", ""), 255), text(message.get("option", ""), 255))
    if kind == "addNote":
        new_id = _clean(message.get("id", ""), 64)
        if not new_id or find_note(scene, new_id) is not None:
            return False
        if len(note_objects(scene)) >= MAX_NOTES:
            return False
        try:
            point = Vector(finite_floats(message.get("position"), 3))
        except ValueError:
            return False
        location = space.inverted_safe() @ point
        target = message.get("object")
        add_note(
            scene,
            location,
            text(message.get("text", ""), MAX_NOTE_LENGTH),
            _one_line(message.get("author", ""), 80),
            note_id=new_id,
            target=_one_line(target, 200) if isinstance(target, str) else None,
            created=_clean(message.get("created") or "", 40) or None,
        )
        return True
    if kind == "removeNote":
        return remove_note(scene, _clean(message.get("id", ""), 64))
    if kind == "addRoomScan":
        return add_room_scan(scene, message, space) is not None
    if kind == "addDecision":
        return add_decision(scene, message) is not None
    if kind == "removeDecision":
        return remove_decision(scene, str(message.get("id", ""))[:64])
    return False
