# SPDX-License-Identifier: GPL-3.0-or-later
"""Changes to the Rhino document: from the app, and from the ProjectAR window.

Mixed into RhinoHost, so it shares its records, layers and materials.

- setMaterial assigns a material (one the document has, or a look made in
  the app, kept as a material with its key so it's reused and updated).
- setVariant shows one option layer of a variant set and hides the others.
- addNote and removeNote add and delete text dots on the ProjectAR Notes layer.
- addRoomScan builds a scanned room as named boxes on a Room Scan layer.
- addDecision and removeDecision keep decisions in the document.
- setCamera moves the active view's camera to where the phone is.
"""

import base64
import datetime
import math
import os
import tempfile
import time
import uuid
from contextlib import contextmanager

import System
import System.Drawing
import Rhino
import Rhino.DocObjects as rd
import Rhino.Geometry as rg

from . import decisions, study
from .protocol import (
    AXIS,
    IDENTITY,
    RX_90,
    as_int,
    clean,
    column,
    finite_floats,
    from_column_major,
    invert,
    matrix_from_message,
    mul,
    normalized,
    one_line,
    text,
    transform_point,
)

NOTES_LAYER = "ProjectAR Notes"
MAX_NOTE_LENGTH = 1000
MAX_NOTES = 1000
PHOTO_SECTION = "ProjectAR Decision Photos"
MAX_KEY = 128
MAX_NAME = 63

SCAN_LAYER = "Room Scan"
# Kind -> (sublayer, material name, sRGB color with alpha)
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
MAX_SCAN_SIZE_METERS = 1000.0


def _rgb(color):
    r, g, b = (int(round(min(max(c, 0.0), 1.0) * 255)) for c in color[:3])
    return System.Drawing.Color.FromArgb(255, r, g, b)


def _finite(value, low=0.0, high=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("not finite")
    return min(max(value, low), high)


def parse_definition(value):
    """A cleaned material definition from the app, or None if it isn't usable."""
    if not isinstance(value, dict):
        return None
    look_key = one_line(value.get("key"), MAX_KEY)
    name = one_line(value.get("name"), MAX_NAME)
    color = value.get("color")
    if not look_key or not name or not isinstance(color, list) or len(color) not in (3, 4):
        return None
    try:
        rgba = [_finite(c) for c in color] + ([1.0] if len(color) == 3 else [])
        metallic = _finite(value.get("metallic", 0.0))
        roughness = _finite(value.get("roughness", 0.5))
    except ValueError:
        return None
    return {"key": look_key, "name": name, "color": rgba, "metallic": metallic, "roughness": roughness}


def _points(values):
    out = System.Collections.Generic.List[rg.Point3d]()
    for p in values:
        out.Add(rg.Point3d(p[0], p[1], p[2]))
    return out


class DocDecisionStore:
    """Decisions in the document's user text, so they travel with the .3dm."""

    def __init__(self, host):
        self._host = host

    def load(self):
        return self._host.get_string("Decisions")

    def save(self, value):
        self._host.set_string("Decisions", value)

    def photo(self, decision_id):
        value = self._host.get_string(decisions.safe_file_id(decision_id), PHOTO_SECTION)
        if not value:
            return None
        try:
            return base64.b64decode(value)
        except ValueError:
            return None

    def set_photo(self, decision_id, data):
        self._host.set_string(decisions.safe_file_id(decision_id), base64.b64encode(data).decode("ascii"), PHOTO_SECTION)

    def remove_photo(self, decision_id):
        self._host.set_string(decisions.safe_file_id(decision_id), None, PHOTO_SECTION)


class EditMixin:
    # ------------------------------------------------------------ helpers

    @property
    def decision_store(self):
        return DocDecisionStore(self)

    @contextmanager
    def undoable(self, name):
        """One undo step for a change made from the ProjectAR window."""
        doc = self.doc
        record = doc.BeginUndoRecord(name) if doc is not None else None
        try:
            yield doc
        finally:
            if record is not None:
                doc.EndUndoRecord(record)
            if doc is not None:
                doc.Views.Redraw()

    def _layer_index(self, name, parent_id=None, color=(0.8, 0.8, 0.8), create=True, unique=False):
        """A layer by name under a parent (top level for None), made if needed.
        With unique, a new layer is always made, numbered if the name is taken."""
        doc = self._doc
        parent = str(parent_id) if parent_id else None
        siblings = {}
        for layer in doc.Layers:
            if layer.IsDeleted:
                continue
            layer_parent = str(layer.ParentLayerId)
            if (parent is None and layer_parent == str(System.Guid.Empty)) or layer_parent == parent:
                siblings[layer.Name] = int(layer.Index)
        if not unique and name in siblings:
            return siblings[name]
        if not create:
            return -1
        final, number = name, 2
        while final in siblings:
            final = f"{name} {number}"
            number += 1
        layer = rd.Layer()
        layer.Name = final
        layer.Color = _rgb(color)
        if parent_id:
            layer.ParentLayerId = parent_id
        return int(doc.Layers.Add(layer))

    def _find_material(self, name):
        doc = self._doc
        for i in range(int(doc.Materials.Count)):
            mat = doc.Materials[i]
            if _real_material(mat, i) and mat.Name == name:
                return i
        return -1

    def _basic_material(self, name, color):
        """A plain material for room scans, made once."""
        index = self._find_material(name)
        if index >= 0:
            return index
        mat = rd.Material()
        mat.Name = name
        mat.DiffuseColor = _rgb(color)
        mat.Transparency = 1.0 - color[3]
        return int(self._doc.Materials.Add(mat))

    def selection_box(self):
        """The bounding box of the selected objects, or None."""
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        box = rg.BoundingBox.Empty
        found = False
        for obj in doc.Objects.GetSelectedObjects(False, False):
            box = rg.BoundingBox.Union(box, obj.Geometry.GetBoundingBox(True))
            found = True
        return box if found and box.IsValid else None

    def selected_labels(self):
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        out = []
        for obj in doc.Objects.GetSelectedObjects(False, False):
            label = self.registry.get(str(obj.Id))
            if label:
                out.append(label)
        return out

    def pick_point(self, prompt):
        """A point picked in a view, or None if cancelled or not possible here."""
        self._picking = True  # changes from the app wait, as they do for commands
        try:
            getter = Rhino.Input.Custom.GetPoint()
            getter.SetCommandPrompt(prompt)
            getter.Get()
            if getter.CommandResult() != Rhino.Commands.Result.Success:
                return None
            p = getter.Point()
            return (float(p.X), float(p.Y), float(p.Z))
        except Exception as exc:
            print(f"ProjectAR: could not pick a point: {exc}")
            return None
        finally:
            self._picking = False

    # ------------------------------------------------------------ messages from the app

    def apply_message(self, message):
        """Applies one message from the app. Unknown types are ignored, so
        newer apps work with this add-on. Returns True if the document changed."""
        kind = message.get("type")
        handler = {
            "setMaterial": self._set_material,
            "setCamera": self._set_camera,
            "setVariant": lambda m: self.apply_variant(text(m.get("set", ""), 255), text(m.get("option", ""), 255)),
            "addNote": self._add_note_from_app,
            "removeNote": lambda m: self.remove_note(clean(m.get("id", ""), 64)),
            "addRoomScan": lambda m: self.add_room_scan(m),
            "addDecision": lambda m: decisions.add_decision(self.decision_store, m) is not None,
            "removeDecision": lambda m: decisions.remove_decision(self.decision_store, str(m.get("id", ""))[:64]),
        }.get(kind)
        if handler is None or self._doc is None:
            return False
        return bool(handler(message))

    # ------------------------------------------------------------ materials

    def _material_target(self, label):
        """What a material change for an object id goes to: ("object", obj)
        or ("leaf", block definition, index of the object in it)."""
        doc = self._doc
        target = self.registry.key_for(label)
        if target is None and "#" in label and "/" in label:
            # An instance id ("instance/source#n"): the source changes, as the app expects.
            target = self.registry.key_for(label.rsplit("#", 1)[0].split("/", 1)[1])
        if isinstance(target, str):
            obj = doc.Objects.FindId(System.Guid(target))
            return ("object", obj) if obj is not None and not obj.IsDeleted else None
        if isinstance(target, tuple) and target[0] == "leaf":
            idef = next((d for d in doc.InstanceDefinitions if d is not None and str(d.Id) == target[1]), None)
            linked = "Linked" in str(getattr(idef, "UpdateType", "")) if idef is not None else False
            if idef is not None and not idef.IsDeleted and not idef.IsReference and not linked:
                return ("leaf", idef, target[2])
        return None

    def _set_material(self, message):
        slot = as_int(message.get("slot"))
        if slot != 0:  # Rhino objects have one material
            return False
        target = self._material_target(str(message.get("object", "")))
        if target is None:
            return False
        changed = False
        if "definition" in message:
            definition = parse_definition(message.get("definition"))
            if definition is None:
                return False
            index, changed = self._ensure_app_material(definition)
            assign = ("index", index)
        else:
            name = str(message.get("material", ""))
            render = self._render_materials.get(name)
            if render is not None:
                assign = ("render", render)
            else:
                index = self._find_material(name)
                if index < 0:
                    return False
                assign = ("index", index)
        return self._assign_material(target, assign) or changed

    @staticmethod
    def _apply_assignment(attrs, assign):
        """Sets a material on attributes. Returns False if it was already set."""
        if assign[0] == "render":
            # RenderMaterial can only be set; it sets the source and index.
            before = (_is_object_source(attrs), int(attrs.MaterialIndex))
            attrs.RenderMaterial = assign[1]
            return (_is_object_source(attrs), int(attrs.MaterialIndex)) != before
        if _is_object_source(attrs) and int(attrs.MaterialIndex) == assign[1]:
            return False
        attrs.MaterialSource = rd.ObjectMaterialSource.MaterialFromObject
        attrs.MaterialIndex = assign[1]
        return True

    def _assign_material(self, target, assign):
        doc = self._doc
        if target[0] == "object":
            obj = target[1]
            attrs = obj.Attributes.Duplicate()
            if not self._apply_assignment(attrs, assign):
                return False
            return bool(doc.Objects.ModifyAttributes(obj, attrs, True))
        # An object inside a block: the block definition is rebuilt with it changed,
        # which changes every instance, like the source object in Blender.
        _, idef, item = target
        objects = idef.GetObjects() or []
        if not 0 <= item < len(objects):
            return False
        geometry = System.Collections.Generic.List[rg.GeometryBase]()
        attributes = System.Collections.Generic.List[rd.ObjectAttributes]()
        changed = False
        for i, obj in enumerate(objects):
            attrs = obj.Attributes.Duplicate()
            if i == item:
                changed = self._apply_assignment(attrs, assign)
            geometry.Add(obj.Geometry.Duplicate())
            attributes.Add(attrs)
        if not changed:
            return False
        return bool(doc.InstanceDefinitions.ModifyGeometry(int(idef.Index), geometry, attributes))

    def _find_app_material(self, look_key):
        doc = self._doc
        for i in range(int(doc.Materials.Count)):
            mat = doc.Materials[i]
            if _real_material(mat, i) and mat.GetUserString(self.MATERIAL_KEY) == look_key:
                return i
        return -1

    def _make_material(self, definition):
        r, g, b, alpha = definition["color"]
        mat = rd.Material()
        mat.Name = definition["name"]
        try:
            mat.ToPhysicallyBased()
            pbr = mat.PhysicallyBased
            pbr.BaseColor = Rhino.Display.Color4f(r, g, b, 1.0)
            pbr.Metallic = definition["metallic"]
            pbr.Roughness = definition["roughness"]
            if alpha < 0.999:
                try:
                    pbr.Alpha = alpha
                except Exception:
                    pbr.Opacity = alpha
        except Exception:  # Rhino without physically based materials
            mat.DiffuseColor = _rgb((r, g, b))
            mat.Transparency = 1.0 - alpha
            mat.Shine = (1.0 - definition["roughness"]) * float(getattr(rd.Material, "MaxShine", 255.0))
        mat.SetUserString(self.MATERIAL_KEY, definition["key"])
        return mat

    def _ensure_app_material(self, definition):
        """The material index for a look from the app, made or updated.
        Returns (index, changed)."""
        doc = self._doc
        index = self._find_app_material(definition["key"])
        new = self._make_material(definition)
        if index < 0:
            return int(doc.Materials.Add(new)), True
        current = self._material_info(doc.Materials[index], doc)
        same = (
            current["name"] == definition["name"]
            and all(abs(a - b) < 2e-3 for a, b in zip(current["color"], definition["color"]))
            and abs(current["metallic"] - definition["metallic"]) < 1e-3
            and abs(current["roughness"] - definition["roughness"]) < 1e-3
        )
        if same:
            return index, False
        doc.Materials.Modify(new, index, True)
        return index, True

    # ------------------------------------------------------------ camera

    def _camera_view(self):
        doc = self._doc
        view = doc.Views.ActiveView
        if view is not None and not isinstance(view, Rhino.Display.RhinoPageView):
            return view
        try:
            views = doc.Views.GetViewList(Rhino.Display.ViewTypeFilter.Model)
        except Exception:  # before Rhino 8
            views = doc.Views.GetViewList(True, False)
        for candidate in views:
            if candidate.ActiveViewport.IsPerspectiveProjection:
                return candidate
        return view

    def _set_camera(self, message):
        """Moves the active view's camera to the device's pose, so a view can
        be framed by walking around the model. The lens is kept."""
        m = matrix_from_message(message.get("m"))
        if m is None:
            return False
        det = (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )
        if abs(det) < 1e-3:
            return False
        pose = mul(invert(self._space) or IDENTITY, m, RX_90, AXIS)
        location = column(pose, 3)
        forward = normalized(tuple(-v for v in column(pose, 2)))
        up = normalized(column(pose, 1))
        if forward is None or up is None:
            return False
        view = self._camera_view()
        if view is None:
            return False
        vp = view.ActiveViewport
        distance = float((vp.CameraLocation - vp.CameraTarget).Length) or 1.0 / self.unit_scale()
        target = tuple(location[i] + forward[i] * distance for i in range(3))
        if vp.IsParallelProjection:
            try:
                vp.ChangeToPerspectiveProjection(True, float(vp.Camera35mmLensLength) or 50.0)
            except Exception:
                pass
        vp.SetCameraLocations(rg.Point3d(*target), rg.Point3d(*location))
        vp.CameraUp = rg.Vector3d(*up)
        view.Redraw()
        return True

    # ------------------------------------------------------------ variants

    def _show_only(self, options, chosen):
        """Shows one option layer and hides the others. Rhino won't hide the
        current layer, so the shown option becomes current if it would be."""
        doc = self._doc
        current = int(doc.Layers.CurrentLayerIndex)
        chosen_index = next(index for name, index in options if name == chosen)
        hidden = set()
        for name, index in options:
            if name != chosen:
                hidden |= self._subtree(index)
        if current in hidden:
            doc.Layers.SetCurrentLayerIndex(chosen_index, True)
        changed = False
        for name, index in options:
            layer = doc.Layers.FindIndex(index)
            want = name == chosen
            if layer is None or bool(layer.IsVisible) == want:
                continue
            layer.IsVisible = want
            try:
                layer.SetPersistentVisibility(want)
            except Exception:
                pass
            doc.Layers.Modify(layer, index, True)
            changed = True
        return changed

    def apply_variant(self, set_name, option_name):
        """Shows one option of a variant set and hides the others. Returns
        False if the set or option doesn't exist or is already showing."""
        options = self._variant_options.get(set_name)
        if not options or option_name not in [name for name, _ in options]:
            return False
        shown = [name for name, index in options if self._layers.get(index) and self._layers[index].own_visible]
        if shown == [option_name]:
            return False
        return self._show_only(options, option_name)

    def mark_variant_set(self):
        """Uses the current layer as a variant set: each of its sublayers is
        one design option. Returns an error to show, or None."""
        doc = self.current()
        index = int(doc.Layers.CurrentLayerIndex)
        layer = self._layers.get(index)
        options = self._children(index) if layer is not None else []
        if len(options) < 2:
            return "Make the parent layer current first. Its sublayers are the options, and it needs two or more."
        current = self.variant_set_ids()
        if layer.id not in current:
            self.set_string("VariantSets", ",".join(current + [layer.id]))
        # Exactly one option showing, so Rhino and the app agree: the one
        # already showing if there is just one, else the first.
        shown = [o.name for o in options if o.own_visible]
        self._show_only([(o.name, o.index) for o in options], shown[0] if len(shown) == 1 else options[0].name)
        self._scene_dirty = True
        return None

    def unmark_variant_set(self, set_name):
        layer_id = self._variant_layer_ids.get(set_name)
        if layer_id is None:
            return False
        self.set_string("VariantSets", ",".join(i for i in self.variant_set_ids() if i != layer_id) or None)
        self._scene_dirty = True
        return True

    def variant_sets(self):
        return list(self._variants)

    # ------------------------------------------------------------ notes

    def _notes_layer(self):
        doc = self._doc
        stored = self.get_string("NotesLayer")
        if stored:
            layer = self._layers_by_id.get(stored)
            if layer is not None:
                return layer.index
        index = self._layer_index(NOTES_LAYER, color=(1.0, 0.55, 0.0))
        self.set_string("NotesLayer", str(doc.Layers.FindIndex(index).Id))
        return index

    def note_records(self):
        return [r for r in self._records.values() if r.kind == "dot" and r.note is not None]

    def notes(self):
        return list(self._notes)

    def add_note(self, note_text, location, author, note_id=None, target=None, created=None):
        """Adds a note as a text dot at a Rhino location. Returns True if added."""
        doc = self._doc
        note_text = str(note_text)[:MAX_NOTE_LENGTH]
        first_line = note_text.strip().splitlines()[0] if note_text.strip() else "Note"
        attrs = rd.ObjectAttributes()
        attrs.LayerIndex = self._notes_layer()
        attrs.Name = f"Note: {first_line}"[:80]
        attrs.SetUserString(self.NOTE_ID, note_id or uuid.uuid4().hex)
        attrs.SetUserString(self.NOTE_AUTHOR, str(author)[:80])
        attrs.SetUserString(self.NOTE_CREATED, created or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        if target:
            attrs.SetUserString(self.NOTE_OBJECT, str(target)[:200])
        dot = rg.TextDot(note_text, rg.Point3d(*location))
        return doc.Objects.AddTextDot(dot, attrs) != System.Guid.Empty

    def _add_note_from_app(self, message):
        new_id = clean(message.get("id", ""), 64)
        if not new_id or any(r.note["id"] == new_id for r in self.note_records()):
            return False
        if len(self.note_records()) >= MAX_NOTES:
            return False
        try:
            position = finite_floats(message.get("position"), 3)
        except ValueError:
            return False
        location = transform_point(invert(self._space) or IDENTITY, position)
        target = message.get("object")
        return self.add_note(
            text(message.get("text", ""), MAX_NOTE_LENGTH),
            location,
            one_line(message.get("author", ""), 80),
            note_id=new_id,
            target=one_line(target, 200) if isinstance(target, str) else None,
            created=clean(message.get("created") or "", 40) or None,
        )

    def _note_record(self, note_id):
        return next((r for r in self.note_records() if r.note["id"] == note_id), None)

    def remove_note(self, note_id):
        rec = self._note_record(note_id)
        if rec is None:
            return False
        return bool(self._doc.Objects.Delete(rec.guid, True))

    def select_note(self, note_id):
        rec = self._note_record(note_id)
        doc = self._doc
        if rec is None or doc is None:
            return False
        doc.Objects.UnselectAll()
        doc.Objects.Select(rec.guid, True)
        doc.Views.Redraw()
        return True

    def add_note_at_selection(self, note_text, author="Rhino"):
        """A note above the selected objects, pointing at the first of them,
        or at the AR origin when nothing is selected."""
        box = self.selection_box()
        labels = self.selected_labels()
        if box is not None:
            c = box.Center
            location = (float(c.X), float(c.Y), float(box.Max.Z))
        else:
            location = self.origin_point() or (0.0, 0.0, 0.0)
        return self.add_note(note_text, location, author, target=labels[0] if labels else None)

    # ------------------------------------------------------------ room scans

    def add_room_scan(self, message):
        """Builds a room scan from the app as named boxes on a new layer.

        Each item has a kind, a RealityKit-space transform (rotation and
        translation in scene units) and dimensions (width, height, depth) in
        scene units. Floors may carry polygon corners in their local space.
        Scan objects are kept out of AR, since the real room is already there.
        """
        items = message.get("items")
        if not isinstance(items, list) or not items:
            return False
        doc = self._doc
        space_inv = invert(self._space) or IDENTITY
        max_size = MAX_SCAN_SIZE_METERS  # the app sends meters; the space turns them into document units
        root_id = None
        subs, counts = {}, {}
        added = 0
        for item in items[:MAX_SCAN_ITEMS]:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if not isinstance(kind, str) or kind not in SCAN_KINDS:
                continue
            try:
                m_rk = from_column_major(finite_floats(item.get("transform"), 16))
                width, height, depth = (min(abs(v), max_size) for v in finite_floats(item.get("dimensions"), 3))
            except ValueError:
                continue
            if root_id is None:
                name = one_line(message.get("name") or SCAN_LAYER, 60) or SCAN_LAYER
                root = self._layer_index(name, color=(0.6, 0.6, 0.6), unique=True)
                root_id = doc.Layers.FindIndex(root).Id
            sub_name, mat_name, color = SCAN_KINDS[kind]
            if sub_name not in subs:
                subs[sub_name] = self._layer_index(sub_name, parent_id=root_id, color=color)
            label = one_line(item.get("category") or kind, 40).replace("_", " ").title() if kind == "object" else kind.title()
            counts[label] = counts.get(label, 0) + 1
            name = label if kind == "floor" and counts[label] == 1 else f"{label} {counts[label]}"
            # RealityKit local (width x, height y, depth z) is Rhino (x, depth y, height z).
            xform = mul(space_inv, m_rk, AXIS)
            geometry = None
            corners = item.get("polygon")
            if kind == "floor" and isinstance(corners, list) and len(corners) >= 3:
                try:
                    local = [finite_floats(c[:3] if isinstance(c, list) else c, 3) for c in corners[:256]]
                    outline = [transform_point(xform, (c[0], -c[2], c[1])) for c in local]
                    polyline = rg.Polyline(_points(outline + [outline[0]]))
                    geometry = rg.Mesh.CreateFromClosedPolyline(polyline)
                except (TypeError, ValueError, IndexError):
                    geometry = None
            if geometry is None:
                x, y, z = (max(float(v), 1e-4) / 2.0 for v in (width, depth, height))
                local = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
                         (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
                geometry = rg.Brep.CreateFromBox(_points([transform_point(xform, p) for p in local]))
            if geometry is None:
                continue
            attrs = rd.ObjectAttributes()
            attrs.Name = name
            attrs.LayerIndex = subs[sub_name]
            attrs.MaterialSource = rd.ObjectMaterialSource.MaterialFromObject
            attrs.MaterialIndex = self._basic_material(mat_name, color)
            attrs.SetUserString(self.HIDE_KEY, "1")
            attrs.SetUserString("ProjectAR.ScanKind", kind)
            if kind == "object":
                attrs.SetUserString("ProjectAR.ScanCategory", one_line(item.get("category", ""), 40))
            if isinstance(geometry, rg.Mesh):
                new_id = doc.Objects.AddMesh(geometry, attrs)
            else:
                new_id = doc.Objects.AddBrep(geometry, attrs)
            if new_id != System.Guid.Empty:
                added += 1
        return added > 0

    # ------------------------------------------------------------ decisions

    def decisions(self):
        return decisions.decisions(self.decision_store)

    def record_decision(self, question, options, chosen, reason, set_name="", criteria=(),
                        confidence=None, author="Rhino", photo=None):
        decision = decisions.record_decision(
            self.decision_store, question, options, chosen, reason, set_name=set_name,
            criteria=criteria, confidence=confidence, author=author, photo=photo,
        )
        if decision is not None and set_name:
            self.apply_variant(set_name, decision["chosen"])
        return decision

    def apply_decision(self, decision_id):
        """Shows the option chosen in a decision. Returns an error to show, or None."""
        decision = decisions.find_decision(self.decision_store, decision_id)
        if decision is None or not decision.get("set"):
            return "This decision isn't about a variant set."
        options = self._variant_options.get(decision["set"])
        if options is None:
            return f"{decision['set']} is no longer a variant set."
        if decision["chosen"] not in [name for name, _ in options]:
            return f"\"{decision['chosen']}\" is no longer in {decision['set']}."
        if self.apply_variant(decision["set"], decision["chosen"]):
            study.log("view.variant", {"set": decision["set"], "option": decision["chosen"], "from": "decision"})
        return None

    def remove_decision(self, decision_id):
        return decisions.remove_decision(self.decision_store, decision_id)

    def decision_photo_file(self, decision_id):
        """The decision's photo as a temporary JPEG file, or None."""
        data = self.decision_store.photo(decision_id)
        if not data:
            return None
        folder = os.path.join(tempfile.gettempdir(), "ProjectAR Decision Photos")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{decisions.safe_file_id(decision_id)}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def export_decisions(self, path):
        return decisions.export_decisions(self.decision_store, self.doc_name(), path)

    def decisions_log(self):
        return decisions.decisions_markdown(self.doc_name(), self.decisions())

    def capture_view(self, max_size=1600):
        """JPEG bytes of the active view as it looks now, or None."""
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        view = doc.Views.ActiveView if doc is not None else None
        if view is None:
            return None
        size = view.ActiveViewport.Size
        width, height = int(size.Width), int(size.Height)
        if width < 2 or height < 2:
            return None
        scale = min(1.0, float(max_size) / max(width, height))
        w, h = max(1, int(round(width * scale))), max(1, int(round(height * scale)))
        path = os.path.join(tempfile.gettempdir(), f"projectar-view-{time.time_ns()}.jpg")
        try:
            bitmap = view.CaptureToBitmap(System.Drawing.Size(w, h))
            if bitmap is None:
                return None
            try:
                bitmap.Save(path, System.Drawing.Imaging.ImageFormat.Jpeg)
            except Exception:  # System.Drawing can't encode here: go through Eto
                import Eto.Drawing as drawing

                Rhino.UI.EtoExtensions.ToEto(bitmap).Save(path, drawing.ImageFormat.Jpeg)
            with open(path, "rb") as f:
                return f.read()
        except Exception as exc:
            print(f"ProjectAR: could not capture the view: {exc}")
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------ the window's tools

    def set_ar_hidden(self, hidden):
        """Keeps the selected objects out of AR (or puts them back). Returns how many changed."""
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        changed = 0
        for obj in list(doc.Objects.GetSelectedObjects(False, False)):
            attrs = obj.Attributes.Duplicate()
            already = (attrs.GetUserString(self.HIDE_KEY) or "") == "1"
            if already == hidden:
                continue
            attrs.SetUserString(self.HIDE_KEY, "1" if hidden else None)
            if doc.Objects.ModifyAttributes(obj, attrs, True):
                changed += 1
        return changed

    def hidden_count(self):
        return sum(1 for r in self._records.values() if r.attrs is not None and r.attrs.ar_hidden)

    def set_origin(self, location):
        self.set_string("Origin", None if location is None else ",".join(repr(float(v)) for v in location))
        self._scene_dirty = True

    def set_origin_from_selection(self):
        """The bottom center of the selection becomes the AR origin, so the
        model stands on the floor where you tap. Returns False with nothing selected."""
        box = self.selection_box()
        if box is None:
            return False
        c = box.Center
        self.set_origin((float(c.X), float(c.Y), float(box.Min.Z)))
        return True


def _real_material(mat, index):
    """False for deleted materials and the empty slots Rhino leaves (a
    detached reference model), which read as the default material."""
    return mat is not None and not mat.IsDeleted and int(getattr(mat, "Index", index)) == index


def _is_object_source(attrs):
    return str(attrs.MaterialSource).split(".")[-1] == "MaterialFromObject"
