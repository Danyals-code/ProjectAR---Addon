# SPDX-License-Identifier: GPL-3.0-or-later
"""The Rhino side of streaming: reads the active document for the streamer.

Rhino differs from Blender in one way that matters here: moving an object
rewrites its geometry instead of changing a transform. Sending the whole mesh
after every move would be slow, so the host watches BeforeTransformObjects.
When a move, rotation or uniform scale replaces an object, the mesh devices
have stays and only its matrix changes (the object's "c" below, the move
since it was last meshed). Any other edit sends the mesh again.

Devices get everything in meters, whatever the document's units, and are
told the unit is 1: the app's lights reach a fixed 20 units and its light
brightness assumes meters, so a millimeter model would otherwise be lit as if
it were a thousand times smaller. Meshes are scaled, matrices carry no scale,
and what comes back from the app is turned into document units.

Everything here runs on Rhino's main thread: document events only note what
changed, and each tick works through those notes.

Objects become:
- breps, extrusions, surfaces, meshes and SubDs: streamed objects;
- block instances: instances, one per object in the block (nested blocks
  flattened), sharing one prototype mesh per block object;
- text dots with a ProjectAR note id: notes;
- lights: lights. Named views and the active view: cameras.
"""

import ctypes
import math
import os
import time

import System
import System.Drawing
import Rhino
import Rhino.DocObjects as rd
import Rhino.Geometry as rg

from . import ids, snapshot, study, textures
from .protocol import (
    AXIS,
    AXIS_INV,
    DEFAULT_MATERIAL,
    IDENTITY,
    Geometry,
    column,
    empty_geometry,
    flatten,
    from_column_major,
    invert,
    is_similarity,
    key,
    look_frame,
    mul,
    scaling,
    transform_point,
    translation,
)
from .rhino_edit import EditMixin
from .streaming import Scene, SceneInstance, SceneObject

SECTION = "ProjectAR"
HIDE_KEY = "ProjectAR.HideInAR"
NOTE_ID = "ProjectAR.NoteId"
NOTE_AUTHOR = "ProjectAR.NoteAuthor"
NOTE_CREATED = "ProjectAR.NoteCreated"
NOTE_OBJECT = "ProjectAR.NoteObject"
MATERIAL_KEY = "ProjectAR.MaterialKey"
SCAN_PREFIX = "ProjectAR Scan "

MATERIAL_CHECK_INTERVAL = 1.0
RENDER_PALETTE_INTERVAL = 10.0
CAMERA_INTERVAL = 0.25
LIGHT_INTERVAL = 1.0
MAX_PALETTE = 200
MAX_LIGHTS = 64
MAX_CAMERAS = 64
MAX_BLOCK_DEPTH = 16
# The app reads Blender's light energies, in meters: watts for point, spot and
# area lights, W/m2 for the sun. A light's own power in watts is used when Rhino
# has one; otherwise its intensity scales Blender's defaults.
POINT_WATTS = 1000.0
AREA_WATTS = 200.0
SUN_STRENGTH = 3.0


# ---------------------------------------------------------------- .NET helpers


_ELEMENTS = {"Single": (4, "f"), "Int32": (4, "i"), "Byte": (1, "B"), "Double": (8, "d")}
_copy_method = None  # the first way of copying that worked, tried first from then on


def net_bytes(array):
    """The raw bytes of a .NET array of floats, ints or bytes. Copying a
    million floats one by one through Python takes a second, so the array is
    pinned and read in one go; slower ways are kept for when that fails."""
    global _copy_method
    count = int(array.Length)
    if count == 0:
        return b""
    size, code = _ELEMENTS[str(array.GetType().GetElementType().Name)]
    interop = System.Runtime.InteropServices

    def pinned():
        handle = interop.GCHandle.Alloc(array, interop.GCHandleType.Pinned)
        try:
            return ctypes.string_at(int(handle.AddrOfPinnedObject().ToInt64()), count * size)
        finally:
            handle.Free()

    def marshal():
        buffer = ctypes.create_string_buffer(count * size)
        interop.Marshal.Copy(array, 0, System.IntPtr(System.Int64(ctypes.addressof(buffer))), count)
        return buffer.raw

    def one_by_one():
        import struct

        return struct.pack(f"<{count}{code}", *list(array))

    methods = [pinned, marshal, one_by_one]
    if _copy_method in methods:
        methods.insert(0, methods.pop(methods.index(_copy_method)))
    for method in methods:
        try:
            data = method()
        except Exception:
            continue
        if len(data) == count * size:
            _copy_method = method
            return data
    raise RuntimeError("could not read mesh data from Rhino")


def to_matrix(xf):
    """A Rhino Transform as rows."""
    return tuple(tuple(float(getattr(xf, f"M{r}{c}")) for c in range(4)) for r in range(4))


def to_transform(m):
    """A Rhino Transform from rows. Transform is a .NET struct, and setting
    its fields from Python could change a copy, so the result is checked; a
    rotation, uniform scale and move can also be built without setting fields."""
    xf = rg.Transform(1.0)
    for r in range(4):
        for c in range(4):
            setattr(xf, f"M{r}{c}", float(m[r][c]))
    if all(abs(a - b) < 1e-9 for row_a, row_b in zip(to_matrix(xf), m) for a, b in zip(row_a, row_b)):
        return xf
    if not is_similarity(m, 1e-5):
        raise RuntimeError("this Rhino can't take that transform")
    x_axis, y_axis = column(m, 0), column(m, 1)
    scale = math.sqrt(sum(v * v for v in x_axis))
    plane = rg.Plane(rg.Point3d.Origin, rg.Vector3d(*[v / scale for v in x_axis]), rg.Vector3d(*[v / scale for v in y_axis]))
    rotate = rg.Transform.PlaneToPlane(rg.Plane.WorldXY, plane)
    resize = rg.Transform.Scale(rg.Point3d.Origin, scale)
    move = rg.Transform.Translation(rg.Vector3d(m[0][3], m[1][3], m[2][3]))
    return move * rotate * resize


def point(p):
    return (float(p.X), float(p.Y), float(p.Z))


def color_floats(c):
    """A System.Drawing.Color as 0 to 1 floats, as Rhino shows it (sRGB)."""
    return [c.R / 255.0, c.G / 255.0, c.B / 255.0]


def _count(items):
    try:
        return len(items)
    except TypeError:
        return 0


def _enum_name(value):
    return str(value).split(".")[-1]


_MESHABLE = ("Brep", "Extrusion", "Mesh", "SubD", "Surface")
_TYPE_NAMES = {"Brep": "Polysurface", "Extrusion": "Extrusion", "Mesh": "Mesh", "SubD": "SubD", "Surface": "Surface"}


def kind_of(obj):
    name = _enum_name(obj.ObjectType)
    if name in _MESHABLE:
        return "mesh"
    if name == "InstanceReference":
        return "block"
    if name == "TextDot":
        return "dot"
    return None


def type_name(obj):
    name = _enum_name(obj.ObjectType)
    if name == "Brep":
        try:
            if obj.Geometry.Faces.Count == 1:
                return "Surface"
        except Exception:
            pass
    if name == "InstanceReference":
        try:
            return obj.InstanceDefinition.Name or "Block"
        except Exception:
            return "Block"
    return _TYPE_NAMES.get(name, name)


def render_mesh(obj, doc):
    """The object's render mesh as one mesh, made if Rhino hasn't yet, or None."""
    geo = obj.Geometry
    joined = rg.Mesh()
    if isinstance(geo, rg.Mesh):
        joined.Append(geo)
        return joined if joined.Faces.Count else None
    params = None
    meshes = None
    try:
        params = doc.GetMeshingParameters(doc.MeshingParameterStyle)
    except Exception:
        params = rg.MeshingParameters.Default
    try:
        meshes = obj.GetMeshes(rg.MeshType.Render)
        if not _count(meshes):
            obj.CreateMeshes(rg.MeshType.Render, params, False)
            meshes = obj.GetMeshes(rg.MeshType.Render)
    except Exception:
        meshes = None
    if not _count(meshes):
        meshes = _mesh_from_geometry(geo, params)
    for m in meshes or []:
        if m is not None:
            joined.Append(m)
    return joined if joined.Faces.Count else None


def _mesh_from_geometry(geo, params):
    try:
        if isinstance(geo, rg.Extrusion):
            geo = geo.ToBrep()
        if isinstance(geo, rg.Brep):
            return list(rg.Mesh.CreateFromBrep(geo, params) or [])
        sub_d = getattr(rg, "SubD", None)
        if sub_d is not None and isinstance(geo, sub_d):
            return [rg.Mesh.CreateFromSubD(geo, 3)]
        if isinstance(geo, rg.Surface):
            return [rg.Mesh.CreateFromSurface(geo, params)]
    except Exception as exc:
        print(f"ProjectAR: could not mesh an object: {exc}")
    return []


def mesh_geometry(mesh, matrix, infos, max_triangles):
    """Triangulates a Rhino mesh into protocol blobs. matrix takes it into
    the space devices expect (RealityKit axes, the object's own frame)."""
    faces = mesh.Faces
    triangles = int(faces.TriangleCount) + 2 * int(faces.QuadCount)
    if triangles == 0:
        return empty_geometry(infos)
    if triangles > max_triangles:
        return empty_geometry(infos, triangles, skipped=True)
    if mesh.Normals.Count != mesh.Vertices.Count:
        mesh.Normals.ComputeNormals()
    if matrix != IDENTITY:
        mesh.Transform(to_transform(matrix))
    count = int(mesh.Vertices.Count)
    blobs = [net_bytes(mesh.Vertices.ToFloatArray()), net_bytes(mesh.Normals.ToFloatArray())]
    has_uvs = count > 0 and mesh.TextureCoordinates.Count == count
    if has_uvs:
        blobs.append(net_bytes(mesh.TextureCoordinates.ToFloatArray()))
    indices = faces.ToIntArray(True)
    index_count = int(indices.Length)
    blobs.append(net_bytes(indices))
    blobs.append(bytes(4 * (index_count // 3)))  # one material per object: slot 0
    return Geometry(materials=infos, triangles=index_count // 3, vertex_count=count,
                    index_count=index_count, has_uvs=has_uvs, blobs=blobs)


def load_texture(source, role, max_size):
    """(format, bytes) for an image file, scaled down with Eto if needed."""
    path = source[1]
    with open(path, "rb") as f:
        data = f.read()
    fmt = textures.image_format(data)
    size = textures.image_size(data)
    if fmt and size and max(size) <= max_size:
        return fmt, data
    import Eto.Drawing as drawing

    image = drawing.Bitmap(path)
    width, height = int(image.Width), int(image.Height)
    if width <= 0 or height <= 0:
        return None
    scale = min(1.0, float(max_size) / max(width, height))
    w, h = max(1, int(round(width * scale))), max(1, int(round(height * scale)))
    if (w, h) != (width, height):
        image = drawing.Bitmap(image, w, h, drawing.ImageInterpolation.High)
    return "png", net_bytes(image.ToByteArray(drawing.ImageFormat.Png))


# ---------------------------------------------------------------- records


class _Attrs:
    """What the host needs from ObjectAttributes, read once."""

    __slots__ = ("layer", "visible", "ar_hidden", "name", "mat_source", "mat_index",
                 "color_source", "color", "model_space")

    def __init__(self, obj):
        a = obj.Attributes
        self.layer = int(a.LayerIndex)
        try:
            self.visible = bool(obj.Visible) and not bool(obj.IsHidden)
        except Exception:
            self.visible = bool(a.Visible)
        self.ar_hidden = (a.GetUserString(HIDE_KEY) or "") == "1"
        self.name = a.Name or ""
        self.mat_source = _enum_name(a.MaterialSource)  # MaterialFromLayer / Object / Parent
        self.mat_index = int(a.MaterialIndex)
        self.color_source = _enum_name(a.ColorSource)  # ColorFromLayer / Object / Material / Parent
        self.color = color_floats(a.ObjectColor)
        self.model_space = _enum_name(a.Space) != "PageSpace"


class _Record:
    """A top-level object the host keeps track of."""

    __slots__ = ("id", "guid", "kind", "label", "serial", "version", "c", "attrs", "selected",
                 "idef", "xform", "note", "matrix_cache")

    def __init__(self, gid, obj):
        self.id = gid
        self.guid = obj.Id
        self.kind = kind_of(obj)
        self.label = ""
        self.serial = None
        self.version = 0
        self.c = IDENTITY  # the move since the mesh devices have was made
        self.attrs = None
        self.selected = False
        self.idef = -1
        self.xform = IDENTITY
        self.note = None
        self.matrix_cache = None


class _Leaf:
    """One object inside a block, with nested blocks flattened."""

    __slots__ = ("obj", "guid", "serial", "path", "xform", "attrs", "chain", "label")

    def __init__(self, obj, path, xform, attrs, chain, label):
        self.obj = obj
        self.guid = str(obj.Id)
        self.serial = int(obj.RuntimeSerialNumber)
        self.path = path
        self.xform = xform
        self.attrs = attrs
        self.chain = chain  # attributes of the blocks around it, innermost first
        self.label = label


class _Layer:
    __slots__ = ("index", "id", "name", "path", "parent", "own_visible", "persistent", "visible", "material", "color", "sort")


# ---------------------------------------------------------------- host


class RhinoHost(EditMixin):
    HIDE_KEY = HIDE_KEY
    NOTE_ID = NOTE_ID
    NOTE_AUTHOR = NOTE_AUTHOR
    NOTE_CREATED = NOTE_CREATED
    NOTE_OBJECT = NOTE_OBJECT
    MATERIAL_KEY = MATERIAL_KEY

    def __init__(self):
        self.textures = textures.TextureStore(1024, load_texture)
        self.registry = ids.IdRegistry()
        self._light_ids = ids.IdRegistry()
        self._camera_ids = ids.IdRegistry()
        self.error = ""
        self._doc = None
        self._doc_serial = None
        self._reset_requested = True
        self._sync_requested = False
        self._records = {}
        self._version_counter = 0
        self._mesh_settings = 0
        self._layers = {}
        self._layers_by_id = {}
        self._leaf_cache = {}
        self._mat_infos = {}
        self._mat_keys = None
        self._palette = []
        self._render_materials = {}  # name -> RenderMaterial, for trying on
        self._render_palette = None
        self._last_render_palette = 0.0
        self._last_material_check = 0.0
        self._space = AXIS
        self._k = 1.0  # document units to meters
        self._scale = IDENTITY  # document units to meters, as a matrix
        self._unscale = IDENTITY
        self._sent_k = None
        self._shape = None
        self._objects = {}
        self._instances = {}
        self._variants = []
        self._variant_options = {}  # set name -> [(option name, layer index)]
        self._variant_layer_ids = {}  # set name -> layer id
        self._notes = []
        self._capped = False
        self._lights = []
        self._lights_key = None
        self._last_lights = 0.0
        self._cameras = []
        self._last_cameras = 0.0
        # Notes from document events, worked through each tick.
        self._touched = set()
        self._geom_dirty = set()
        self._replacing = set()
        self._armed = {}
        self._moved = {}
        self._attr_dirty = set()
        self._scene_dirty = True
        self._layers_dirty = True
        self._materials_dirty = True
        self._lights_dirty = True
        self._selection_dirty = False
        self._command_depth = 0
        self._picking = False  # a point is being picked for the window
        self._last_command_event = time.monotonic()
        self._undo_record = None
        self._subscriptions = []
        self._subscribe_all()

    # ------------------------------------------------------------ events

    def _subscribe_all(self):
        doc_events = (
            ("AddRhinoObject", self._on_add),
            ("DeleteRhinoObject", self._on_delete),
            ("UndeleteRhinoObject", self._on_undelete),
            ("ReplaceRhinoObject", self._on_replace),
            ("ModifyObjectAttributes", self._on_attributes),
            ("BeforeTransformObjects", self._on_before_transform),
            ("SelectObjects", self._on_select),
            ("DeselectObjects", self._on_deselect),
            ("DeselectAllObjects", self._on_deselect_all),
            ("LayerTableEvent", self._on_layers),
            ("MaterialTableEvent", self._on_materials),
            ("RenderMaterialsTableEvent", self._on_materials),
            ("InstanceDefinitionTableEvent", self._on_blocks),
            ("LightTableEvent", self._on_lights),
            ("DocumentPropertiesChanged", self._on_properties),
            ("EndOpenDocument", self._on_open),
            ("NewDocument", self._on_document),
            ("ActiveDocumentChanged", self._on_document),
            ("EndSaveDocument", self._on_saved),
        )
        for name, handler in doc_events:
            self._subscribe(Rhino.RhinoDoc, name, handler)
        self._subscribe(Rhino.Render.RenderContent, "ContentChanged", self._on_materials)
        self._subscribe(Rhino.Commands.Command, "BeginCommand", self._on_begin_command)
        self._subscribe(Rhino.Commands.Command, "EndCommand", self._on_end_command)
        self._subscribe(Rhino.Commands.Command, "UndoRedo", self._on_undo_redo)

    def _subscribe(self, owner, name, handler):
        try:
            event = getattr(owner, name)
        except AttributeError:
            print(f"ProjectAR: this Rhino has no {name} event")
            return
        def guarded(sender, e, handler=handler, name=name):
            try:
                handler(sender, e)
            except Exception as exc:  # never into Rhino's own code
                print(f"ProjectAR: {name} handler: {exc}")

        try:
            event += guarded
            self._subscriptions.append((owner, name, guarded))
        except Exception as exc:
            print(f"ProjectAR: could not watch {name}: {exc}")

    def close(self):
        for owner, name, handler in self._subscriptions:
            try:
                event = getattr(owner, name)
                event -= handler
            except Exception:
                pass
        self._subscriptions = []

    # Handlers only take notes: they run inside Rhino's own operations.

    def _on_add(self, sender, e):
        try:
            gid = str(e.ObjectId)
            self._touched.add(gid)
            if gid in self._replacing:
                self._replacing.discard(gid)  # the new half of a replace
            else:
                self._geom_dirty.add(gid)
        except Exception:
            pass

    def _on_delete(self, sender, e):
        try:
            self._touched.add(str(e.ObjectId))
        except Exception:
            pass

    def _on_undelete(self, sender, e):
        try:
            gid = str(e.ObjectId)
            self._touched.add(gid)
            self._geom_dirty.add(gid)
        except Exception:
            pass

    def _on_replace(self, sender, e):
        try:
            gid = str(e.ObjectId)
            self._touched.add(gid)
            self._replacing.add(gid)
            move = self._armed.pop(gid, None)
            if move is not None:
                self._moved[gid] = mul(move, self._moved.get(gid, IDENTITY))
            else:
                self._geom_dirty.add(gid)
        except Exception:
            pass

    def _on_attributes(self, sender, e):
        try:
            gid = str(e.RhinoObject.Id)
            self._touched.add(gid)
            self._attr_dirty.add(gid)
        except Exception:
            pass

    def _on_before_transform(self, sender, e):
        try:
            if e.ObjectsWillBeCopied or int(e.GripCount) > 0:
                return
            move = to_matrix(e.Transform)
            if not is_similarity(move):
                return
            for obj in e.Objects or []:
                self._armed[str(obj.Id)] = move
        except Exception:
            pass

    def _on_select(self, sender, e):
        self._set_selected(e, True)

    def _on_deselect(self, sender, e):
        self._set_selected(e, False)

    def _set_selected(self, e, selected):
        try:
            for obj in e.RhinoObjects or []:
                rec = self._records.get(str(obj.Id))
                if rec is not None:
                    rec.selected = selected
            self._selection_dirty = True
        except Exception:
            pass

    def _on_deselect_all(self, sender, e):
        for rec in self._records.values():
            rec.selected = False
        self._selection_dirty = True

    def _on_layers(self, sender, e):
        self._layers_dirty = True
        self._lights_dirty = True
        study.note_edit("layer.edit", "")

    def _on_materials(self, sender, e):
        self._materials_dirty = True
        study.note_edit("material.edit", "")

    def _on_blocks(self, sender, e):
        self._leaf_cache.clear()
        self._scene_dirty = True
        study.note_edit("block.edit", "")

    def _on_lights(self, sender, e):
        self._lights_dirty = True

    def _on_properties(self, sender, e):
        # Units, mesh quality and the like: meshes may differ now.
        self._mesh_settings += 1
        self._scene_dirty = True

    def _on_open(self, sender, e):
        # Import and reference files open "into" this document: their objects
        # arrive as ordinary additions.
        if getattr(e, "Merge", False) or getattr(e, "Reference", False):
            return
        self._on_document(sender, e)

    def _on_document(self, sender, e):
        self._reset_requested = True
        try:
            study.log("file.open", {"file": self.doc_file_name()})
        except Exception:
            pass

    def _on_saved(self, sender, e):
        try:
            study.log("file.save", {"file": self.doc_file_name()})
        except Exception:
            pass

    def _on_begin_command(self, sender, e):
        self._command_depth += 1
        self._last_command_event = time.monotonic()

    def _on_end_command(self, sender, e):
        self._command_depth = max(0, self._command_depth - 1)
        self._last_command_event = time.monotonic()
        try:
            name = str(e.CommandEnglishName)
            if name not in ("Undo", "Redo"):
                study.log("command", {"name": name, "result": _enum_name(e.CommandResult)})
        except Exception:
            pass

    def _on_undo_redo(self, sender, e):
        try:
            if e.IsEndUndo:
                study.log("edit.undo")
            elif e.IsEndRedo:
                study.log("edit.redo")
        except Exception:
            pass

    # ------------------------------------------------------------ streamer API

    def take_sync_request(self):
        requested, self._sync_requested = self._sync_requested, False
        return requested

    def can_edit(self):
        """Changes from the app wait while a Rhino command runs, so they
        don't cut into what the user is doing."""
        if self._command_depth and time.monotonic() - self._last_command_event > 600:
            self._command_depth = 0  # an end of command was missed
        return self._command_depth == 0 and not self._picking

    def begin_edits(self):
        doc = self.doc
        self._undo_record = doc.BeginUndoRecord("ProjectAR") if doc is not None else None

    def end_edits(self, changed):
        doc = self.doc
        if doc is None:
            return
        if self._undo_record is not None:
            try:
                doc.EndUndoRecord(self._undo_record)
            except Exception:
                pass
            self._undo_record = None
        if changed:
            doc.Views.Redraw()

    def on_app_change(self, message):
        study.log_app_message(message)

    @property
    def doc(self):
        return self._active_doc()

    def current(self):
        """The active document with layers and records up to date, for the
        window's actions, which can run between ticks."""
        doc = self._active_doc()
        if doc is not None:
            self._process_events(doc)
            if self._layers_dirty:
                self._layers_dirty = False
                self._load_layers(doc)
        return doc

    def scene(self, opts):
        doc = self._active_doc()
        if doc is None:
            return Scene()
        now = time.monotonic()
        self._process_events(doc)
        if self._layers_dirty:
            self._layers_dirty = False
            self._load_layers(doc)
            self._scene_dirty = True
        if self._materials_dirty or now - self._last_material_check >= MATERIAL_CHECK_INTERVAL:
            if self._refresh_materials(doc, now):
                self._scene_dirty = True
        space = self._space_for(doc, opts)
        if self._sent_k is not None and self._k != self._sent_k:
            self._sync_requested = True  # other units: every mesh is scaled differently
        self._sent_k = self._k
        if self._selection_dirty:
            self._selection_dirty = False
            if opts.scope == "SELECTED":
                self._scene_dirty = True
        shape = (opts.scope, opts.layer, opts.include_instances, opts.max_instances, space)
        if self._scene_dirty or shape != self._shape:
            self._scene_dirty = False
            self._shape = shape
            self._space = space
            self._build_scene(doc, opts, space)
        lights = self._current_lights(doc, opts, space, now) if opts.stream_lights else []
        cameras = self._current_cameras(doc, space, now) if opts.stream_lights else []
        return Scene(
            name=self.doc_name(),
            unit_scale=1.0,  # everything is sent in meters
            objects=self._objects,
            instances=self._instances,
            variants=self._variants,
            notes=self._notes,
            lights=lights,
            cameras=cameras,
            palette=self._palette,
            instances_capped=self._capped,
        )

    # ------------------------------------------------------------ document

    def _active_doc(self):
        doc = Rhino.RhinoDoc.ActiveDoc
        if doc is None:
            return None
        serial = int(doc.RuntimeSerialNumber)
        if serial != self._doc_serial or self._reset_requested:
            self._reset(doc, serial)
        self._doc = doc
        return doc

    def _reset(self, doc, serial):
        """Starts over with a document: another file, a new one, or the
        first tick. Devices get a full sync."""
        self._sync_requested = self._doc_serial is not None  # the first document isn't a switch
        self._doc = doc
        self._doc_serial = serial
        self._reset_requested = False
        self._records.clear()
        self.registry.clear()
        self._leaf_cache.clear()
        self._mat_infos.clear()
        self._mat_keys = None
        self._render_palette = None
        for pending in (self._touched, self._geom_dirty, self._replacing, self._attr_dirty):
            pending.clear()
        self._armed.clear()
        self._moved.clear()
        self._load_layers(doc)
        self._refresh_materials(doc, time.monotonic())
        settings = rd.ObjectEnumeratorSettings()
        settings.HiddenObjects = True
        settings.LockedObjects = True
        settings.NormalObjects = True
        settings.IncludeLights = False
        settings.IncludeGrips = False
        settings.ReferenceObjects = True
        settings.DeletedObjects = False
        for obj in doc.Objects.GetObjectList(settings):
            if kind_of(obj) is not None:
                self._add_record(obj)
        self._scene_dirty = True
        self._lights_dirty = True
        self._shape = None

    def doc_name(self):
        doc = self._doc
        name = (doc.Name or "") if doc is not None else ""
        if name.lower().endswith(".3dm"):
            name = name[:-4]
        return name or "Untitled"

    def doc_file_name(self):
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        return (doc.Name or "unsaved") if doc is not None else "unsaved"

    def doc_path(self):
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        return (doc.Path or "") if doc is not None else ""

    def unit_scale(self):
        doc = self._doc
        if doc is None:
            return 1.0
        try:
            scale = float(Rhino.RhinoMath.UnitScale(doc.ModelUnitSystem, Rhino.UnitSystem.Meters))
        except Exception:
            scale = 1.0
        return scale if math.isfinite(scale) and scale > 0 else 1.0

    def get_string(self, entry, section=SECTION):
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        if doc is None:
            return None
        try:
            value = doc.Strings.GetValue(section, entry)
        except Exception:
            return None
        return value or None

    def set_string(self, entry, value, section=SECTION):
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        if doc is None:
            return
        if value is None:
            try:
                doc.Strings.Delete(section, entry)
            except Exception:
                pass
        else:
            doc.Strings.SetString(section, entry, value)

    # ------------------------------------------------------------ records

    def _next_version(self):
        self._version_counter += 1
        return self._version_counter

    def _add_record(self, obj):
        gid = str(obj.Id)
        rec = _Record(gid, obj)
        rec.version = self._next_version()
        self._refresh_record(rec, obj)
        self._records[gid] = rec
        return rec

    def _refresh_record(self, rec, obj):
        rec.guid = obj.Id
        rec.serial = int(obj.RuntimeSerialNumber)
        rec.attrs = _Attrs(obj)
        try:
            rec.selected = int(obj.IsSelected(False)) > 0
        except Exception:
            rec.selected = False
        if rec.kind == "block":
            try:
                rec.idef = int(obj.InstanceDefinition.Index)
                rec.xform = to_matrix(obj.InstanceXform)
            except Exception:
                rec.idef = -1
        elif rec.kind == "dot":
            rec.note = None
            a = obj.Attributes
            note_id = a.GetUserString(NOTE_ID) or ""
            if note_id:
                dot = obj.Geometry
                rec.note = {
                    "id": note_id[:64],
                    "text": str(dot.Text or ""),
                    "author": a.GetUserString(NOTE_AUTHOR) or "",
                    "created": a.GetUserString(NOTE_CREATED) or "",
                    "point": point(dot.Point),
                    "object": a.GetUserString(NOTE_OBJECT) or "",
                }
        rec.label = self.registry.label(rec.id, rec.attrs.name or type_name(obj))
        rec.matrix_cache = None

    def _process_events(self, doc):
        touched = self._touched | self._attr_dirty
        if not touched:
            self._armed.clear()
            return
        geom, moved = self._geom_dirty, self._moved
        self._touched, self._geom_dirty, self._attr_dirty, self._moved = set(), set(), set(), {}
        self._replacing.clear()
        self._armed.clear()
        self._scene_dirty = True
        for gid in touched:
            rec = self._records.get(gid)
            obj = doc.Objects.FindId(rec.guid if rec is not None else System.Guid(gid))
            if obj is None or obj.IsDeleted or kind_of(obj) is None:
                if rec is not None:
                    del self._records[gid]
                    study.note_edit("object.delete", rec.label)
                continue
            if rec is None:
                rec = self._add_record(obj)
                study.note_edit("object.add", rec.label)
                continue
            if gid in geom:
                rec.version = self._next_version()
                rec.c = IDENTITY
                study.note_edit("object.geometry", rec.label)
            elif gid in moved:
                combined = mul(moved[gid], rec.c)
                if is_similarity(combined, 1e-5):
                    rec.c = combined
                else:  # rounding added up: start from the geometry again
                    rec.version = self._next_version()
                    rec.c = IDENTITY
                study.note_edit("object.transform", rec.label)
            self._refresh_record(rec, obj)

    # ------------------------------------------------------------ layers

    def _load_layers(self, doc):
        layers, by_id = {}, {}
        for layer in doc.Layers:
            try:
                if layer.IsDeleted:
                    continue
                item = _Layer()
                item.index = int(layer.Index)
                item.id = str(layer.Id)
                item.name = layer.Name or ""
                item.path = layer.FullPath or item.name
                item.parent = str(layer.ParentLayerId)
                item.own_visible = bool(layer.IsVisible)
                try:
                    # What the layer shows when its parent turns on: Rhino turns
                    # sublayers of a hidden layer off, and remembers this.
                    item.persistent = bool(layer.GetPersistentVisibility())
                except Exception:
                    item.persistent = item.own_visible
                item.material = int(layer.RenderMaterialIndex)
                item.color = color_floats(layer.Color)
                try:
                    item.sort = int(layer.SortIndex)
                except Exception:
                    item.sort = item.index
                layers[item.index] = item
                by_id[item.id] = item
            except Exception as exc:
                print(f"ProjectAR: skipped a layer: {exc}")
        for item in layers.values():
            parent = by_id.get(item.parent)
            item.parent = parent.index if parent is not None else None
        resolved = {}

        def visible(item, depth=0):
            """On, and every layer above it on. A sublayer of a hidden layer
            counts by its persistent visibility, as Rhino shows it once the
            parent is on (variant options rely on this)."""
            if item.index in resolved:
                return resolved[item.index]
            parent = layers.get(item.parent) if item.parent is not None else None
            if parent is None or depth > 64:
                result = item.own_visible
            else:
                parent_on = visible(parent, depth + 1)
                if not parent_on:
                    item.own_visible = item.persistent
                result = parent_on and item.own_visible
            resolved[item.index] = result
            return result

        for item in layers.values():
            item.visible = visible(item)
        self._layers, self._layers_by_id = layers, by_id
        self._lights_dirty = True

    def _children(self, index):
        out = [l for l in self._layers.values() if l.parent == index]
        out.sort(key=lambda l: (l.sort, l.index))
        return out

    def _subtree(self, index):
        out, stack = set(), [index]
        while stack:
            current = stack.pop()
            if current in out:
                continue
            out.add(current)
            stack.extend(l.index for l in self._layers.values() if l.parent == current)
        return out

    def _visible_below(self, index, top):
        """True if every layer from index up to, but not including, top is on."""
        current, depth = self._layers.get(index), 0
        while current is not None and current.index != top and depth < 64:
            if not current.own_visible:
                return False
            current = self._layers.get(current.parent) if current.parent is not None else None
            depth += 1
        return current is not None

    def layer_paths(self):
        return sorted(l.path for l in self._layers.values())

    # ------------------------------------------------------------ materials

    def _read_render_materials(self, doc):
        """(name, render material, info) for the Materials panel's materials."""
        out, seen = [], set()
        generation = Rhino.Render.RenderTexture.TextureGeneration.Allow
        try:
            for rm in doc.RenderMaterials:
                name = rm.Name or ""
                if not name or name in seen or name.startswith(SCAN_PREFIX):
                    continue
                seen.add(name)
                info = None
                if len(out) < MAX_PALETTE:
                    convert = getattr(rm, "ToMaterial", None) or rm.SimulatedMaterial  # ToMaterial is Rhino 8's
                    mat = convert(generation)
                    if mat is not None:
                        info = self._material_info(mat, doc)
                        info["name"] = name
                out.append((name, rm, info))
        except Exception as exc:
            print(f"ProjectAR: could not list render materials: {exc}")
        return out

    def _refresh_materials(self, doc, now):
        """Reads every material again, so edits Rhino doesn't announce (an
        image file changed on disk) still arrive. Returns True if any
        changed."""
        dirty = self._materials_dirty
        self._materials_dirty = False
        self._last_material_check = now
        self.textures.begin_epoch()
        infos, palette, render = {}, [], {}
        try:
            count = int(doc.Materials.Count)
        except Exception:
            count = 0
        for index in range(count):
            try:
                mat = doc.Materials[index]
                if mat is None or mat.IsDeleted or int(getattr(mat, "Index", index)) != index:
                    continue  # deleted, or an empty slot that reads as the default material
                infos[index] = self._material_info(mat, doc)
            except Exception as exc:
                print(f"ProjectAR: could not read a material: {exc}")
        seen = set()
        if dirty or self._render_palette is None or now - self._last_render_palette >= RENDER_PALETTE_INTERVAL:
            # Turning a render material into a plain one can bake its
            # textures, so this runs on changes, not every second.
            self._render_palette = self._read_render_materials(doc)
            self._last_render_palette = now
        for name, rm, info in self._render_palette:
            seen.add(name)
            render[name] = rm
            if info is not None and len(palette) < MAX_PALETTE:
                palette.append(info)
        for info in infos.values():
            name = info["name"]
            if name not in seen and not name.startswith(SCAN_PREFIX) and len(palette) < MAX_PALETTE:
                seen.add(name)
                palette.append(info)
        self._render_materials = render
        keys = (key(infos), key(palette))
        changed = keys != self._mat_keys
        self._mat_keys = keys
        self._mat_infos = infos
        self._palette = palette
        return changed

    def _material_info(self, mat, doc):
        name = mat.Name or "Material"
        pbr = None
        try:
            if mat.IsPhysicallyBased:
                pbr = mat.PhysicallyBased
        except Exception:
            pbr = None
        if pbr is not None:
            c = pbr.BaseColor
            rgb = [float(c.R), float(c.G), float(c.B)]
            metallic = float(pbr.Metallic)
            roughness = float(pbr.Roughness)
            alpha = float(getattr(pbr, "Alpha", 1.0))
            opacity = float(getattr(pbr, "Opacity", 1.0))
            if opacity < 1.0:  # glass: see-through, but not invisible
                alpha *= 0.3 + 0.7 * opacity
        else:
            rgb = color_floats(mat.DiffuseColor)
            alpha = 1.0 - float(mat.Transparency)
            metallic = 0.0
            max_shine = float(getattr(rd.Material, "MaxShine", 255.0)) or 255.0
            roughness = 1.0 - float(mat.Shine) / max_shine
        info = {
            "name": name,
            "color": [min(max(v, 0.0), 1.0) for v in rgb] + [min(max(alpha, 0.0), 1.0)],
            "metallic": min(max(metallic, 0.0), 1.0),
            "roughness": min(max(roughness, 0.0), 1.0),
        }
        try:
            app_key = mat.GetUserString(MATERIAL_KEY)
        except Exception:
            app_key = None
        if app_key:
            info["key"] = app_key
        maps = self._material_textures(mat, doc)
        if maps:
            info["textures"] = maps
        return info

    def _material_textures(self, mat, doc):
        tt = rd.TextureType
        wanted = (
            ("baseColor", ("PBR_BaseColor", "Bitmap")),
            ("roughness", ("PBR_Roughness",)),
            ("metallic", ("PBR_Metallic",)),
        )
        maps, base_path = {}, None
        for role, names in wanted:
            for name in names:
                which = getattr(tt, name, None)
                if which is None:
                    continue
                path = self._texture_path(mat, which, doc)
                if path:
                    maps[role] = self.textures.register(_file_source(path), role)
                    if role == "baseColor":
                        base_path = path
                    break
        if base_path:
            for name in ("PBR_Alpha", "Transparency"):
                which = getattr(tt, name, None)
                if which is not None and self._texture_path(mat, which, doc) == base_path:
                    maps["alpha"] = True
                    break
        return maps

    @staticmethod
    def _texture_path(mat, which, doc):
        try:
            tex = mat.GetTexture(which)
        except Exception:
            return None
        if tex is None or not getattr(tex, "Enabled", True):
            return None
        candidates = []
        try:
            ref = tex.FileReference
            if ref is not None:
                candidates += [ref.FullPath, ref.RelativePath]
        except Exception:
            pass
        candidates.append(getattr(tex, "FileName", None))
        folder = os.path.dirname(doc.Path) if doc.Path else ""
        for path in candidates:
            if not path:
                continue
            path = str(path)
            if os.path.isfile(path):
                return path
            if folder:
                nearby = os.path.join(folder, os.path.basename(path.replace("\\", "/")))
                if os.path.isfile(nearby):
                    return nearby
        return None

    def _resolve_material(self, attrs_chain, top):
        """The material index for an object, following "by parent" through
        the blocks around it to the top-level instance. -1 is none. The
        outermost object has no parent, so its own setting counts."""
        chain = list(attrs_chain) + ([top] if top is not None else [])
        for i, attrs in enumerate(chain):
            if attrs.mat_source == "MaterialFromParent" and i < len(chain) - 1:
                continue
            if attrs.mat_source == "MaterialFromLayer":
                layer = self._layers.get(attrs.layer)
                return layer.material if layer is not None else -1
            return attrs.mat_index
        return -1

    def _resolve_color(self, attrs_chain, top):
        chain = list(attrs_chain) + ([top] if top is not None else [])
        for i, attrs in enumerate(chain):
            if attrs.color_source == "ColorFromParent" and i < len(chain) - 1:
                continue
            if attrs.color_source == "ColorFromObject":
                return attrs.color
            if attrs.color_source == "ColorFromMaterial":
                index = self._resolve_material([attrs], None)
                info = self._mat_infos.get(index)
                if info is not None:
                    return info["color"][:3]
            layer = self._layers.get(attrs.layer)
            return layer.color if layer is not None else [0.8, 0.8, 0.8]
        return [0.8, 0.8, 0.8]

    def _info(self, attrs_chain, top=None):
        """The material info for an object: its material, or without one,
        its display color, which is what most Rhino models are colored by."""
        index = self._resolve_material(attrs_chain, top)
        info = self._mat_infos.get(index) if index >= 0 else None
        if info is not None:
            return info
        color = self._resolve_color(attrs_chain, top)
        return {"name": "Display Color", "color": list(color) + [1.0], "metallic": 0.0, "roughness": 0.5}

    # ------------------------------------------------------------ scene

    def _space_for(self, doc, opts):
        """Rhino world coordinates to what devices get: meters, RealityKit
        axes, from the AR origin."""
        k = self.unit_scale()
        self._k, self._scale, self._unscale = k, scaling(k), scaling(1.0 / k)
        origin = self.origin_point() if opts.origin == "POINT" else None
        move = translation(-origin[0], -origin[1], -origin[2]) if origin is not None else IDENTITY
        return mul(AXIS, self._scale, move)

    def _place(self, space, world):
        """A matrix for devices from a Rhino world matrix. Meshes are already
        in meters, so the scale to meters is taken out again: the app gets
        rotation and translation (in meters) only."""
        return flatten(mul(space, world, AXIS_INV, self._unscale))

    def origin_point(self):
        value = self.get_string("Origin")
        if not value:
            return None
        try:
            x, y, z = (float(v) for v in value.split(","))
        except ValueError:
            return None
        return (x, y, z) if all(math.isfinite(v) for v in (x, y, z)) else None

    def _matrix(self, rec, space):
        cache = rec.matrix_cache
        if cache is not None and cache[0] is space and cache[1] is rec.c:
            return cache[2]
        value = self._place(space, rec.c)
        rec.matrix_cache = (space, rec.c, value)
        return value

    def _variant_sets(self):
        """[(set layer, [option layers])] for the layers marked as variant sets."""
        out = []
        for layer_id in self.variant_set_ids():
            layer = self._layers_by_id.get(layer_id)
            if layer is None:
                continue
            options = self._children(layer.index)
            if options:
                out.append((layer, options))
        return out

    def variant_set_ids(self):
        value = self.get_string("VariantSets")
        if not value:
            return []
        return [v for v in value.split(",") if v]

    def _build_scene(self, doc, opts, space):
        layers = self._layers
        sets = self._variant_sets()
        names = [s.name for s, _ in sets]
        # Layer names repeat under different parents, so repeats use the full path.
        set_name = {s.index: (s.name if names.count(s.name) == 1 else s.path) for s, _ in sets}
        # Every option of a shown variant set is streamed, hidden or not, so
        # the app can switch between them instantly.
        option_of = {}  # layer index -> option layer index
        for set_layer, options in sets:
            if not set_layer.visible:
                continue
            for option in options:
                for index in self._subtree(option.index):
                    option_of[index] = option.index

        if opts.scope == "LAYER":
            target = next((l for l in layers.values() if l.path == opts.layer), None)
            scope_layers = self._subtree(target.index) if target is not None else set()
        else:
            scope_layers = None

        def shown(attrs):
            if not attrs.visible or not attrs.model_space:
                return False
            layer = layers.get(attrs.layer)
            if layer is None:
                return False
            if layer.visible:
                return True
            option = option_of.get(attrs.layer)
            return option is not None and self._visible_below(attrs.layer, option)

        objects, instances, notes = {}, {}, []
        capped = False
        members = {}  # layer index -> labels, for variant options
        for rec in self._records.values():
            attrs = rec.attrs
            if rec.kind == "dot":
                if rec.note is not None:
                    notes.append(self._note(rec, space))
                continue
            members.setdefault(attrs.layer, []).append(rec.label)
            if attrs.ar_hidden or not shown(attrs):
                continue
            if opts.scope == "SELECTED" and not rec.selected:
                continue
            if scope_layers is not None and attrs.layer not in scope_layers:
                continue
            if rec.kind == "mesh":
                info = self._info([attrs])
                objects[rec.label] = SceneObject(
                    version=(rec.version, self._mesh_settings, self._k),
                    matrix=self._matrix(rec, space),
                    material_key=key(info),
                    materials=(lambda info=info: [info]),
                    build=(lambda budget, gid=rec.id: self._build_object(gid, budget)),
                )
            elif rec.kind == "block" and opts.include_instances and not capped:
                capped = self._gather_block(doc, rec, space, instances, opts.max_instances, shown)

        variants, self._variant_options, self._variant_layer_ids = [], {}, {}
        for set_layer, options in sets:
            active = next((o.name for o in options if o.own_visible), options[0].name)
            entries = []
            for option in options:
                labels = []
                for index in self._subtree(option.index):
                    labels += members.get(index, [])
                entries.append({"name": option.name, "objects": sorted(labels)})
            name = set_name[set_layer.index]
            variants.append({"name": name, "active": active, "options": entries})
            self._variant_options[name] = [(o.name, o.index) for o in options]
            self._variant_layer_ids[name] = set_layer.id

        notes.sort(key=lambda n: (n["created"], n["id"]))
        self._objects, self._instances, self._notes = objects, instances, notes
        self._variants, self._capped = variants, capped

    def _note(self, rec, space):
        p = transform_point(space, rec.note["point"])
        note = {
            "id": rec.note["id"],
            "text": rec.note["text"],
            "author": rec.note["author"],
            "created": rec.note["created"],
            "position": [round(v, 5) for v in p],
        }
        if rec.note["object"]:
            note["object"] = rec.note["object"]
        return note

    def _gather_block(self, doc, rec, space, instances, limit, shown):
        """Adds a block instance's objects as instances. Returns True when
        the limit is reached."""
        for leaf in self._leaves(doc, rec.idef, 0):
            if not shown(leaf.attrs) or leaf.attrs.ar_hidden:
                continue
            if any(not a.visible or a.ar_hidden for a in leaf.chain):
                continue
            if len(instances) >= limit:
                return True
            index = self._resolve_material([leaf.attrs] + leaf.chain, rec.attrs)
            info = self._info([leaf.attrs] + leaf.chain, rec.attrs)
            proto_key = ("leaf", leaf.guid, leaf.serial, index, key(info["color"]) if index < 0 else "",
                         self._mesh_settings, self._k)
            iid = f"{rec.label}/{leaf.label}#{leaf.path}"
            instances[iid] = SceneInstance(
                proto_key=proto_key,
                matrix=self._place(space, mul(rec.xform, leaf.xform)),
                source=leaf.label,
                material_key=key(info),
                materials=(lambda info=info: [info]),
                build=(lambda budget, leaf=leaf, info=info: self._build_leaf(leaf, info, budget)),
            )
        return False

    def _leaves(self, doc, idef_index, depth):
        cached = self._leaf_cache.get(idef_index)
        if cached is not None:
            return cached
        leaves = []
        if depth <= MAX_BLOCK_DEPTH and idef_index >= 0:
            try:
                idef = doc.InstanceDefinitions[idef_index]
            except Exception:
                idef = None
            if idef is not None and not idef.IsDeleted:
                for i, obj in enumerate(idef.GetObjects() or []):
                    kind = kind_of(obj)
                    try:
                        attrs = _Attrs(obj)
                    except Exception:
                        continue
                    if kind == "block":
                        try:
                            nested = int(obj.InstanceDefinition.Index)
                            xform = to_matrix(obj.InstanceXform)
                        except Exception:
                            continue
                        for leaf in self._leaves(doc, nested, depth + 1):
                            leaves.append(_Leaf(leaf.obj, f"{i}.{leaf.path}", mul(xform, leaf.xform),
                                                leaf.attrs, leaf.chain + [attrs], leaf.label))
                    elif kind == "mesh":
                        name = attrs.name or f"{idef.Name} {type_name(obj)}"
                        label = self.registry.label(("leaf", str(idef.Id), i), name)
                        leaves.append(_Leaf(obj, str(i), IDENTITY, attrs, [], label))
        self._leaf_cache[idef_index] = leaves
        return leaves

    # ------------------------------------------------------------ meshes

    def _build_object(self, gid, budget):
        rec = self._records.get(gid)
        if rec is None:
            return empty_geometry([dict(DEFAULT_MATERIAL)])
        infos = [self._info([rec.attrs])]
        doc = self._doc
        obj = doc.Objects.FindId(rec.guid) if doc is not None else None
        if obj is None:
            return empty_geometry(infos)
        try:
            mesh = render_mesh(obj, doc)
            if mesh is None:
                return empty_geometry(infos)
            undo_move = invert(rec.c) or IDENTITY
            return mesh_geometry(mesh, mul(self._scale, AXIS, undo_move), infos, budget)
        except Exception as exc:
            print(f"ProjectAR: could not mesh {rec.label}: {exc}")
            return empty_geometry(infos)

    def _build_leaf(self, leaf, info, budget):
        try:
            mesh = render_mesh(leaf.obj, self._doc)
            if mesh is None:
                return empty_geometry([info])
            return mesh_geometry(mesh, mul(self._scale, AXIS), [info], budget)
        except Exception as exc:
            print(f"ProjectAR: could not mesh {leaf.label}: {exc}")
            return empty_geometry([info])

    # ------------------------------------------------------------ lights and cameras

    def _current_lights(self, doc, opts, space, now):
        if not self._lights_dirty and now - self._last_lights < LIGHT_INTERVAL and self._lights_key == space:
            return self._lights
        self._lights_dirty = False
        self._last_lights = now
        self._lights_key = space
        lights = []
        try:
            count = int(doc.Lights.Count)
        except Exception:
            count = 0
        for i in range(count):
            if len(lights) >= MAX_LIGHTS:
                break
            try:
                obj = doc.Lights[i]
                if obj is None or obj.IsDeleted:
                    continue
                attrs = _Attrs(obj)
                layer = self._layers.get(attrs.layer)
                if not attrs.visible or attrs.ar_hidden or layer is None or not layer.visible:
                    continue
                if opts.scope == "SELECTED" and not obj.IsSelected(False):
                    continue
                info = self._light(obj.LightGeometry, str(obj.Id), attrs.name, space)
                if info is not None:
                    lights.append(info)
            except Exception as exc:
                print(f"ProjectAR: skipped a light: {exc}")
        try:
            sun = doc.Lights.Sun
            if sun is not None and sun.Enabled:
                light = sun.Light
                strength = float(getattr(sun, "Intensity", 0.0) or 0.0)  # Rhino 7 and later
                info = self._light(light, "sun", "Sun", space, force_kind="sun", sun_intensity=strength)
                if info is not None:
                    lights.append(info)
        except Exception:
            pass
        lights.sort(key=lambda item: item["id"])
        self._lights = lights
        return lights

    @staticmethod
    def _energy(light, kind, sun_intensity=0.0):
        """The light's energy as the app reads it: Blender's units, in meters."""
        intensity = max(float(light.Intensity), 0.0)
        if kind == "sun":
            return SUN_STRENGTH * (sun_intensity if sun_intensity > 0 else intensity)
        try:
            watts = float(light.PowerWatts)
        except Exception:
            watts = 0.0
        if math.isfinite(watts) and watts > 0:
            return watts
        return intensity * (AREA_WATTS if kind == "area" else POINT_WATTS)

    def _light(self, light, gid, name, space, force_kind=None, sun_intensity=0.0):
        if light is None or (not force_kind and not light.IsEnabled):
            return None
        if force_kind:
            kind = force_kind
        elif light.IsSpotLight:
            kind = "spot"
        elif light.IsDirectionalLight:
            kind = "sun"
        elif light.IsRectangularLight or light.IsLinearLight:
            kind = "area"
        elif light.IsPointLight:
            kind = "point"
        else:
            return None
        location = point(light.Location)
        direction = point(light.Direction)
        if kind == "area":
            length, width = point(light.Length), point(light.Width)
            center = tuple(location[k] + (length[k] + width[k]) / 2.0 for k in range(3))
            frame = look_frame(center, direction, width) or translation(*center)
            size = [math.sqrt(sum(v * v for v in length)), math.sqrt(sum(v * v for v in width))]
        else:
            frame = look_frame(location, direction, (0.0, 0.0, 1.0)) if kind != "point" else None
            frame = frame or translation(*location)
            size = None
        info = {
            "id": self._light_ids.label(gid, name or f"{kind.title()} Light"),
            "kind": kind,
            "color": [round(v, 5) for v in color_floats(light.Diffuse)],
            "energy": round(self._energy(light, kind, sun_intensity), 5),
            "shadow": float(getattr(light, "ShadowIntensity", 1.0)) > 0.0,
            "m": self._place(space, frame),
        }
        if kind in ("point", "spot"):
            info["radius"] = 0.0
        if kind == "spot":
            info["spotSize"] = round(2.0 * float(light.SpotAngleRadians), 5)
            info["spotBlend"] = round(min(max(1.0 - float(light.HotSpot), 0.0), 1.0), 5)
        if size is not None:
            info["size"] = [round(v * self._k, 5) for v in size]
        return info

    def _current_cameras(self, doc, space, now):
        if now - self._last_cameras < CAMERA_INTERVAL:
            return self._cameras
        self._last_cameras = now
        cameras = []
        try:
            view = doc.Views.ActiveView
            if view is not None:
                vp = view.ActiveViewport
                info = self._camera(rd.ViewportInfo(vp), ("view", str(vp.Name)), str(vp.Name), True, space)
                if info is not None:
                    cameras.append(info)
            for i in range(int(doc.NamedViews.Count)):
                if len(cameras) >= MAX_CAMERAS:
                    break
                named = doc.NamedViews[i]
                info = self._camera(named.Viewport, ("named", str(named.Name)), str(named.Name), False, space)
                if info is not None:
                    cameras.append(info)
        except Exception as exc:
            print(f"ProjectAR: could not read the views: {exc}")
        self._cameras = cameras
        return cameras

    def _camera(self, vi, camera_key, name, active, space):
        frame = look_frame(point(vi.CameraLocation), point(vi.CameraDirection), point(vi.CameraUp))
        if frame is None:
            return None
        near, far = float(vi.FrustumNear), float(vi.FrustumFar)
        info = {
            "id": self._camera_ids.label(camera_key, name),
            "active": active,
            "aspect": round(float(vi.FrustumAspect) or 1.0, 5),
            "clip": [round(near * self._k, 5), round(far * self._k, 5)],
            "m": self._place(space, frame),
        }
        top, bottom = float(vi.FrustumTop), float(vi.FrustumBottom)
        if vi.IsPerspectiveProjection and near > 0:
            info["projection"] = "perspective"
            info["fovY"] = round(2.0 * math.atan(((top - bottom) / 2.0) / near), 6)
        else:
            info["projection"] = "orthographic"
            info["orthoScale"] = round((float(vi.FrustumRight) - float(vi.FrustumLeft)) * self._k, 5)
        return info

    # ------------------------------------------------------------ snapshot

    def export_snapshot(self, scene, opts):
        """The streamed scene as USDZ, in world space like the Blender
        add-on's (the AR origin is the app's business)."""
        to_world = mul(AXIS, self._scale, invert(self._space) or IDENTITY)
        items = []
        for name, obj in scene.objects.items():
            matrix = flatten(mul(to_world, from_column_major(obj.matrix)))
            items.append((name, obj.build(10_000_000), matrix))
        for name, inst in scene.instances.items():
            matrix = flatten(mul(to_world, from_column_major(inst.matrix)))
            items.append((name, inst.build(10_000_000), matrix))
        if not items:
            raise snapshot.SnapshotError("nothingToExport")
        try:
            return snapshot.build_usdz(items, self.textures.data, 1.0)  # meshes are in meters
        except snapshot.SnapshotError:
            raise
        except Exception as exc:
            raise snapshot.SnapshotError("exportFailed", str(exc)) from exc

    # ------------------------------------------------------------ study

    def view_state(self):
        """The active view's camera, rounded so jitter doesn't count."""
        doc = self._doc or Rhino.RhinoDoc.ActiveDoc
        try:
            vp = doc.Views.ActiveView.ActiveViewport
            location, direction = point(vp.CameraLocation), point(vp.CameraDirection)
            return (str(vp.Name), tuple(round(v, 3) for v in location), tuple(round(v, 3) for v in direction),
                    round(float(vp.Camera35mmLensLength), 2))
        except Exception:
            return None

    def study_host(self):
        return {"device": "Rhino", "rhino_version": str(Rhino.RhinoApp.Version), "file": self.doc_path()}


def _file_source(path):
    try:
        stat = os.stat(path)
        return ("file", path, int(stat.st_mtime), int(stat.st_size))
    except OSError:
        return ("file", path, 0, 0)
