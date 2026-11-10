# SPDX-License-Identifier: GPL-3.0-or-later
"""Tracks scene changes and turns them into ProjectAR protocol frames.

Everything goes to devices in meters, and "hello" says the unit scale is 1.
The app's lights reach a fixed 20 units and its light brightness assumes
meters, so a scene with a Unit Scale of 0.001 (millimeters) would otherwise
be lit as if it were a thousand times smaller. Meshes are scaled to meters,
matrices carry no extra scale, and what the app sends back (notes, room
scans, camera poses) is in meters and turned back into Blender units.

Each device gets what it can read. Apps that list "instancing" in their
"client" message get one prototype mesh per unique instanced geometry plus
small "instances" entries, and older apps one mesh per instance. Apps that
list "materialUpdates" get "objectMaterials" when only materials changed, and
older apps the whole mesh again.
"""

import collections
import json
import math
import struct
import time
import uuid
from dataclasses import dataclass, field

import bpy
import numpy as np
from mathutils import Matrix

from . import extras, materials, review, snapshot, study, textures
from .materials import DEFAULT_MATERIAL, material_info, slot_names
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
CLIENT_SNAPSHOT = snapshot.FEATURE
PROTOTYPE_PREFIX = "~mesh/"

SUPPORTED_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
GEOMETRY_MIN_INTERVAL = 0.1  # max 10 mesh resends per second per object
PING_INTERVAL = 2.0
# How long a device waits for its "client" message before it's known by its IP.
IDENTIFY_WAIT = 2.0
# Unanswered requests to connect are turned away after this long.
APPROVAL_TIMEOUT = 120.0
# A refused device is refused again without asking for this long.
DENIED_COOLDOWN = 60.0
MAX_REQUESTS = 3
# How often every streamed material is compared with what devices have, so
# changes Blender's update notifications miss (undo, drivers, some node group
# edits) still arrive. The list of scene materials is rebuilt at the same time.
MATERIAL_CHECK_INTERVAL = 1.0
# Painted images are resent once painting pauses for this long.
IMAGE_SETTLE = 0.75
MAX_PALETTE = 200
# Texture bytes queued per device per tick, so meshes and pings keep flowing.
TEXTURE_BYTES_PER_TICK = 4 * 1024 * 1024
# Textures wait while a device still has this much to receive.
TEXTURE_BACKLOG_LIMIT = 8 * 1024 * 1024
# Snapshot chunks are paced the same way.
SNAPSHOT_BYTES_PER_TICK = 4 * 1024 * 1024
# Materials the add-on makes for room scans aren't design materials.
_SCAN_PREFIX = "ProjectAR Scan "

# Depsgraph pads unused levels of ObjectInstance.persistent_id with this.
UNUSED_PERSISTENT_ID = 2**31 - 1

# Blender (x, y, z) -> RealityKit (x, z, -y). See protocol section 4.4.
AXIS = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))
AXIS_INV = AXIS.inverted()


@dataclass
class Options:
    """What to stream and how. Read from the add-on settings every tick."""

    max_triangles: int = 300_000
    include_instances: bool = True
    max_instances: int = 2000
    scope: str = "VISIBLE"  # VISIBLE, SELECTED or COLLECTION
    collection: str = ""
    origin: str = "WORLD"  # WORLD or CURSOR
    paused: bool = False
    max_texture_size: int = 1024
    # Ask in Blender before a new device gets the scene or can change it.
    require_approval: bool = True
    # Device keys allowed before, so they aren't asked again.
    trusted: frozenset = frozenset()
    stream_lights: bool = True


@dataclass
class Geometry:
    materials: list
    triangles: int = 0
    skipped: bool = False
    vertex_count: int = 0
    index_count: int = 0
    has_uvs: bool = False
    blobs: list = field(default_factory=list)


@dataclass
class _Instance:
    matrix: list
    frame: bytes = None  # one-mesh-per-instance frame, when one was built this tick
    changed: bool = False  # True if existing devices need that frame
    proto: str = None  # prototype mesh id, for apps that use instancing
    source: str = None  # the Blender object a material change goes to


@dataclass
class _ProtoTick:
    """What happened to prototypes in one tick."""

    built: dict = field(default_factory=dict)  # id -> frame, new geometry
    materials: dict = field(default_factory=dict)  # id -> infos, material-only change
    material_frames: dict = field(default_factory=dict)  # id -> frame, for apps without materialUpdates
    removed: list = field(default_factory=list)
    mapping: list = field(default_factory=list)  # instances entries that are new or changed


# ---------------------------------------------------------------- helpers


def _send_approval(client, state):
    client.send(_json_frame({"type": "approval", "state": state}), force=True)


def _finite(value):
    """The value with any NaN or infinity replaced by 0, which JSON can't carry."""
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _json_frame(message):
    try:
        data = json.dumps(message, separators=(",", ":"), allow_nan=False)
    except ValueError:
        # The app rejects NaN, so a damaged value must not take the whole message with it.
        data = json.dumps(_finite(message), separators=(",", ":"), allow_nan=False)
    return encode_frame(KIND_JSON, data.encode("utf-8"))


def _key(value):
    """A comparable text for a message part, to notice changes."""
    return json.dumps(_finite(value), sort_keys=True, separators=(",", ":"))


def _hidden_in(obj, view_layer):
    """Hidden with H (the eye), or not in the view layer at all."""
    try:
        return obj.hide_get(view_layer=view_layer)
    except RuntimeError:
        return True


# Time spent on messages from the app per tick, so a flood can't freeze Blender.
MESSAGE_BUDGET = 0.05
MAX_WAITING_MESSAGES = 500


def _flatten(m):
    return [round(m[row][col], 6) for col in range(4) for row in range(4)]


def _to_realitykit(points):
    out = points[:, [0, 2, 1]]
    out[:, 2] *= -1.0
    return out


def _persistent_id(instance):
    pid = tuple(instance.persistent_id)
    if UNUSED_PERSISTENT_ID in pid:
        pid = pid[: pid.index(UNUSED_PERSISTENT_ID)]
    return pid


def _slot_infos(obj_eval):
    return [material_info(s.material) for s in obj_eval.material_slots] or [dict(DEFAULT_MATERIAL)]


def read_geometry(obj_eval, max_triangles, unit=1.0):
    """Triangulate an evaluated object (or instance) into protocol blobs,
    in meters: unit is meters per Blender unit."""
    geometry = Geometry(materials=_slot_infos(obj_eval))
    mesh = obj_eval.to_mesh()
    try:
        if mesh is None:
            return geometry
        if hasattr(mesh, "calc_loop_triangles"):
            mesh.calc_loop_triangles()
        n_tris = len(mesh.loop_triangles)
        geometry.triangles = n_tris
        geometry.skipped = n_tris > max_triangles
        if not n_tris or geometry.skipped:
            return geometry

        n_loops = len(mesh.loops)
        vertex_index = np.empty(n_loops, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", vertex_index)
        coords = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", coords)
        positions = _to_realitykit(coords.reshape(-1, 3)[vertex_index])
        if unit != 1.0:
            positions *= unit

        normals = np.empty(n_loops * 3, dtype=np.float32)
        mesh.corner_normals.foreach_get("vector", normals)
        normals = _to_realitykit(normals.reshape(-1, 3))

        blobs = geometry.blobs
        blobs.append(positions.astype("<f4").tobytes())
        blobs.append(normals.astype("<f4").tobytes())

        uv_layer = mesh.uv_layers.active
        if uv_layer is not None:
            uvs = np.empty(n_loops * 2, dtype=np.float32)
            uv_layer.data.foreach_get("uv", uvs)
            blobs.append(uvs.astype("<f4").tobytes())
            geometry.has_uvs = True

        indices = np.empty(n_tris * 3, dtype=np.int32)
        mesh.loop_triangles.foreach_get("loops", indices)
        face_materials = np.empty(n_tris, dtype=np.int32)
        mesh.loop_triangles.foreach_get("material_index", face_materials)
        np.clip(face_materials, 0, len(geometry.materials) - 1, out=face_materials)

        blobs.append(indices.astype("<u4").tobytes())
        blobs.append(face_materials.astype("<u4").tobytes())

        geometry.vertex_count = n_loops
        geometry.index_count = n_tris * 3
        return geometry
    finally:
        obj_eval.to_mesh_clear()


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
    header_bytes = json.dumps(_finite(header), separators=(",", ":")).encode("utf-8")
    payload = b"".join(
        [struct.pack("<I", len(header_bytes)), header_bytes, *geometry.blobs]
    )
    return encode_frame(KIND_MESH, payload)


def _push_undo():
    """One undo step for what the app changed this tick. Without it, Ctrl+Z on
    an earlier edit would silently throw away what the app added. Skipped
    outside Object Mode and while an operator like a transform is running,
    where a step would cut into the user's own work."""
    context = bpy.context
    if getattr(context, "mode", "OBJECT") != "OBJECT":
        return
    if any(window.modal_operators for window in context.window_manager.windows):
        return
    try:
        bpy.ops.ed.undo_push(message="ProjectAR")
    except Exception:  # no window, as in background mode
        pass


def _scope_filter(view_layer, opts):
    """Return a predicate for objects (or instancers) the user wants in AR."""
    if opts.scope == "SELECTED":

        def in_scope(obj):
            return obj.select_get(view_layer=view_layer)

    elif opts.scope == "COLLECTION":
        coll = bpy.data.collections.get(opts.collection)
        names = {o.name_full for o in coll.all_objects} if coll else set()

        def in_scope(obj):
            return obj.name_full in names

    else:

        def in_scope(obj):
            return True

    return lambda obj: getattr(obj, "viewar_visible", True) and in_scope(obj)


def _evaluated(obj, depsgraph):
    # Hidden variant options in excluded collections aren't evaluated.
    try:
        return obj.evaluated_get(depsgraph)
    except (RuntimeError, ReferenceError):
        return obj


def _clean_id(value):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isalnum() or ch in "-_.")[:64]


# ---------------------------------------------------------------- streamer


class SceneStreamer:
    def __init__(self, server, options):
        self.server = server
        self._options = options  # callable returning Options, read every tick
        self._dirty = set()  # ids whose geometry must be sent again
        self._material_dirty = set()  # ids whose materials may have changed
        self._known = set()
        self._sent_matrices = {}
        self._world_cache = {}  # id -> (world matrix, converted matrix)
        self._space = None
        self._unit = 1.0  # meters per Blender unit
        self._unscale = Matrix.Identity(4)
        self._inbox = collections.deque()  # (client, message) waiting to be applied
        self._last_geometry = {}
        self._triangles = {}
        self._last_ping = 0.0
        self._budget = None
        self._sync_key = None
        self._variants_key = None
        self._notes_key = None
        self._lights_key = None
        self._cameras_key = None
        self._palette = []
        self._palette_key = None
        self._last_material_check = 0.0
        self._material_keys = {}  # material full name -> its info when last checked
        self._sent_materials = {}  # id -> slot materials last sent, as a key
        self._slot_counts = {}  # id -> slot count of the last mesh sent
        self._slot_names = {}  # object id -> slot material names when last sent
        # Prototypes: geometry key -> id, id -> latest frame, id -> triangles.
        self._protos = {}
        self._proto_frames = {}
        self._proto_triangles = {}
        self._proto_counter = 0
        self._protos_stale = False  # rebuilt for everyone after a pause
        self._instance_proto = {}  # instance id -> prototype id devices have
        self._proto_tick = _ProtoTick()
        self._painted = {}  # image name -> when its pixels last changed
        self.textures = textures.TextureStore(1024)
        materials.texture_store = self.textures
        # Snapshots: exports to run, and files on their way to devices.
        self._snapshot_jobs = []
        self._snapshot_sends = []
        self.skipped = set()
        self.clients = []
        # Devices waiting to be allowed, newest last, and ones refused recently.
        self.requests = []
        self._denied = {}  # device key -> time it may ask again
        self.on_request = None  # called with a client when Blender should ask
        self.object_count = 0
        self.instance_count = 0
        self.prototype_count = 0
        self.instances_capped = False
        # Set while a snapshot exports, so its scene updates aren't taken for edits.
        self._exporting = False

    # stats for the UI
    @property
    def client_labels(self):
        return [c.label for c in self.clients]

    @property
    def triangle_count(self):
        return sum(self._triangles.values())

    @property
    def snapshot_clients(self):
        return [c for c in self.clients if c.approval == "allowed" and CLIENT_SNAPSHOT in c.features]

    @property
    def snapshot_busy(self):
        return bool(self._snapshot_jobs or self._snapshot_sends)

    def request_full_sync(self):
        for client in self.server.live_clients():
            client.needs_full_sync = True

    def on_undo(self):
        """Undo and redo can change materials without telling the depsgraph
        handler, so check them on the next tick."""
        self._last_material_check = 0.0

    def _snapshot_pending_for(self, client):
        return any(j["client"] is client for j in self._snapshot_jobs) or any(
            s["client"] is client for s in self._snapshot_sends
        )

    def request_snapshot(self, snapshot_id=None, client=None):
        """Queues a USDZ snapshot for one device, or every device that can take one."""
        self._snapshot_jobs.append({"id": snapshot_id or str(uuid.uuid4()).upper(), "client": client})

    # called from depsgraph_update_post (main thread)
    def on_depsgraph_update(self, scene, depsgraph):
        if self._exporting:
            return
        changed_materials, datas = set(), set()
        for update in depsgraph.updates:
            block = getattr(update.id, "original", update.id)
            if isinstance(block, bpy.types.Object):
                if update.is_updated_geometry:
                    self._dirty.add(block.name_full)
                elif update.is_updated_shading:
                    self._material_dirty.add(block.name_full)
            elif isinstance(block, bpy.types.Material):
                changed_materials.add(block.name_full)
            elif isinstance(block, bpy.types.Image):
                self._painted[block.name_full] = time.monotonic()
            elif isinstance(block, bpy.types.ShaderNodeTree):
                # Shader Editor edits can report the node tree instead of the
                # material, and node group edits only the group.
                changed_materials |= materials.materials_using(block)
            elif update.is_updated_geometry and isinstance(
                block, (bpy.types.Mesh, bpy.types.Curve, bpy.types.MetaBall)
            ):
                datas.add(block.name_full)

        if not (changed_materials or datas):
            return
        # All objects, not just the scene's: instanced collections often
        # live outside the scene.
        for obj in bpy.data.objects:
            if obj.type not in SUPPORTED_TYPES:
                continue
            if obj.data is not None and obj.data.name_full in datas:
                self._dirty.add(obj.name_full)
            elif changed_materials and any(
                s.material is not None and s.material.name_full in changed_materials
                for s in obj.material_slots
            ):
                self._material_dirty.add(obj.name_full)

    # called from bpy.app.timers (main thread)
    def tick(self):
        now = time.monotonic()
        clients = self.server.live_clients()
        self.clients = clients
        room = MAX_WAITING_MESSAGES - len(self._inbox)
        if room > 0:
            self._inbox.extend(self.server.take_messages(limit=room))
        if not clients and not self._inbox:
            self.requests = []
            self._snapshot_jobs.clear()
            self._snapshot_sends.clear()
            return

        opts = self._options()
        context = bpy.context
        scene = context.scene
        view_layer = context.view_layer or scene.view_layers[0]

        # Blender world coordinates to what devices get: meters, RealityKit
        # axes, from the AR origin.
        unit = extras.meters(scene)
        space = AXIS @ Matrix.Scale(unit, 4)
        if opts.origin == "CURSOR":
            space = space @ scene.cursor.matrix.inverted_safe()
        if space != self._space:
            self._space = space
            self._unit = unit
            self._unscale = Matrix.Scale(1.0 / unit, 4)
            self._world_cache.clear()

        self._handle_messages(scene, view_layer)
        self._update_approvals(clients, opts, now)
        # Only allowed devices get the scene.
        clients = [c for c in clients if c.approval == "allowed"]
        self._settle_painted_images(now)
        if not clients:
            return
        if opts.max_texture_size != self.textures.max_size:
            self.textures.set_max_size(opts.max_texture_size)
            for client in clients:
                client.sent_textures.clear()
        depsgraph = context.evaluated_depsgraph_get()

        # "hello" carries the scene name and unit scale, so resend it on change.
        sync_key = (scene.name_full, scene.unit_settings.scale_length)
        if self._sync_key is not None and sync_key != self._sync_key:
            self.request_full_sync()
            if sync_key[1] != self._sync_key[1]:
                self._protos_stale = True  # every mesh is scaled differently now
        self._sync_key = sync_key
        # Objects skipped under the old triangle budget may fit now, and vice versa.
        if self._budget is not None and opts.max_triangles != self._budget:
            self._dirty |= self._known
            self._protos_stale = True
        self._budget = opts.max_triangles

        existing = [c for c in clients if not c.needs_full_sync]
        fresh = [c for c in clients if c.needs_full_sync]
        head = []
        if now - self._last_ping > PING_INTERVAL:
            self._last_ping = now
            head.append(_json_frame({"type": "ping"}))

        accept = _scope_filter(view_layer, opts)
        if opts.paused and not fresh:
            self._protos_stale = True
            for client in existing:
                for frame in head:
                    client.send(frame)
            self._run_snapshot_job(scene, view_layer, accept, clients)
            self._send_snapshots()
            return

        # While paused, new devices still get the current scene, but state is
        # left alone so existing devices catch up on everything after resuming.
        commit = not opts.paused
        if not commit:
            self._protos_stale = True
        variants = review.variant_sets(scene, view_layer)
        notes = review.notes(scene, space)
        # Every option of a variant set is streamed, hidden or not, so the
        # app can switch between them instantly.
        # Objects hidden themselves, or in a hidden variant set, stay out.
        variant_objects = review.variant_object_names(review.shown_variant_sets(variants, view_layer))
        objects = {
            o.name_full: o
            for o in scene.objects
            if o.type in SUPPORTED_TYPES
            and accept(o)
            and (
                o.visible_get(view_layer=view_layer)
                or (o.name_full in variant_objects and not o.hide_viewport and not _hidden_in(o, view_layer))
            )
        }

        tail = []
        check_slots = commit and (fresh or now - self._last_material_check >= MATERIAL_CHECK_INTERVAL)
        if check_slots:
            self._check_materials(objects, depsgraph, now, tail)

        uses_protos = [CLIENT_INSTANCING in c.features for c in clients]
        flat = not all(uses_protos)
        proto = any(uses_protos)
        fresh_flat = any(CLIENT_INSTANCING not in c.features for c in fresh)
        refresh_protos = commit and self._protos_stale and proto
        if commit:
            self._protos_stale = False
        instances = self._gather_instances(
            depsgraph, accept, opts, now, commit=commit, flat=flat, fresh_flat=fresh_flat,
            proto=proto, refresh_protos=refresh_protos, check_slots=check_slots,
        )
        protos = self._proto_tick
        self.object_count = len(objects)
        self.instance_count = len(instances)
        matrices = {n: self._convert(n, o.matrix_world) for n, o in objects.items()}
        matrices.update((i, entry.matrix) for i, entry in instances.items())
        built = {}
        object_frames = []
        material_entries = []
        material_meshes = []

        if commit:
            current = matrices.keys()
            removed = self._known - current
            added = current - self._known
            self._known = set(current)
            for name in removed:
                self._forget(name)
            if removed:
                head.append(_json_frame({"type": "remove", "ids": sorted(removed)}))

            self._dirty |= added & objects.keys()
            for name in list(self._dirty):
                if name in instances or name.startswith(PROTOTYPE_PREFIX):
                    continue  # rate limited, retried by _gather_instances
                obj = objects.get(name)
                if obj is None:
                    self._dirty.discard(name)
                    continue
                if now - self._last_geometry.get(name, 0.0) < GEOMETRY_MIN_INTERVAL:
                    continue  # rate limited, try again next tick
                self._dirty.discard(name)
                self._material_dirty.discard(name)
                self._last_geometry[name] = now
                if existing:  # fresh clients get everything in their full sync
                    built[name] = self._object_frame(name, obj, depsgraph, opts)
                    object_frames.append(built[name])

            legacy = any(CLIENT_MATERIAL_UPDATES not in c.features for c in existing)
            for name in list(self._material_dirty):
                obj = objects.get(name)
                if obj is None or name in instances or name.startswith(PROTOTYPE_PREFIX):
                    continue
                if name in self._dirty:
                    self._material_dirty.discard(name)
                    continue
                if now - self._last_geometry.get(name, 0.0) < GEOMETRY_MIN_INTERVAL:
                    continue
                self._material_dirty.discard(name)
                source = _evaluated(obj, depsgraph)
                infos = _slot_infos(source)
                self._slot_names[name] = slot_names(source)
                if len(infos) != self._slot_counts.get(name):
                    self._dirty.add(name)  # different slots: the mesh goes next tick
                    continue
                key = _key(infos)
                if key == self._sent_materials.get(name):
                    continue
                self._last_geometry[name] = now
                self._sent_materials[name] = key
                material_entries.append({"id": name, "materials": infos})
                if legacy:
                    material_meshes.append(self._object_frame(name, obj, depsgraph, opts))
            # Sources of instances that aren't streamed themselves were handled
            # by _gather_instances; anything else that's gone is forgotten.
            self._material_dirty = {
                n for n in self._material_dirty
                if n in objects or n in instances or n.startswith(PROTOTYPE_PREFIX)
            }

            changed = []
            for name, matrix in matrices.items():
                if self._sent_matrices.get(name) != matrix:
                    self._sent_matrices[name] = matrix
                    changed.append({"id": name, "m": matrix})
            transforms = [_json_frame({"type": "transforms", "objects": changed})] if changed else []
        else:
            transforms = []

        # While paused, existing devices stay as they are. The keys stay
        # old, so they get these changes once updates resume.
        variants_key = _key(variants)
        if commit and variants_key != self._variants_key:
            self._variants_key = variants_key
            tail.append(_json_frame({"type": "variants", "sets": variants}))
        notes_key = _key(notes)
        if commit and notes_key != self._notes_key:
            self._notes_key = notes_key
            tail.append(_json_frame({"type": "notes", "notes": notes}))
        lights, cameras = (
            extras.lights_and_cameras(scene, view_layer, accept, space) if opts.stream_lights else ([], [])
        )
        lights_key, cameras_key = _key(lights), _key(cameras)
        if commit and lights_key != self._lights_key:
            self._lights_key = lights_key
            tail.append(_json_frame({"type": "lights", "lights": lights}))
        if commit and cameras_key != self._cameras_key:
            self._cameras_key = cameras_key
            tail.append(_json_frame({"type": "cameras", "cameras": cameras}))

        if commit:
            flat_frames = [e.frame for e in instances.values() if e.changed]
            proto_frames = []
            if protos.removed:
                proto_frames.append(_json_frame({"type": "remove", "ids": sorted(protos.removed)}))
            proto_frames.extend(protos.built.values())
            if protos.mapping:
                proto_frames.append(_json_frame({"type": "instances", "instances": protos.mapping}))
            proto_material_entries = [{"id": pid, "materials": infos} for pid, infos in protos.materials.items()]
            for client in existing:
                updates = CLIENT_MATERIAL_UPDATES in client.features
                frames = head + object_frames
                if CLIENT_INSTANCING in client.features:
                    frames = frames + proto_frames
                    entries = material_entries + proto_material_entries
                    if not updates:
                        frames = frames + list(protos.material_frames.values())
                else:
                    frames = frames + flat_frames
                    entries = material_entries
                if updates:
                    if entries:
                        frames = frames + [_json_frame({"type": "objectMaterials", "objects": entries})]
                else:
                    frames = frames + material_meshes
                for frame in frames + transforms + tail:
                    client.send(frame)
        for client in fresh:
            self._full_sync(client, scene, depsgraph, opts, objects, instances, matrices, built)
            client.send(_json_frame({"type": "variants", "sets": variants}), force=True)
            client.send(_json_frame({"type": "notes", "notes": notes}), force=True)
            client.send(_json_frame({"type": "materials", "materials": self._palette}), force=True)
            client.send(_json_frame({"type": "lights", "lights": lights}), force=True)
            client.send(_json_frame({"type": "cameras", "cameras": cameras}), force=True)
        self._send_textures(clients)
        self._run_snapshot_job(scene, view_layer, accept, clients)
        self._send_snapshots()

    def _check_materials(self, objects, depsgraph, now, tail):
        """Rebuilds the scene's material list and marks objects whose
        materials changed since devices got them."""
        self._last_material_check = now
        # Every material in use registers its textures again here, so ids no
        # longer used (old versions, other files) stop being sent.
        self.textures.begin_epoch()
        palette, keys = self._scene_materials()
        changed = {n for n, k in keys.items() if n in self._material_keys and self._material_keys[n] != k}
        self._material_keys = keys
        for name, obj in objects.items():
            if name not in self._slot_names:
                continue
            names = slot_names(_evaluated(obj, depsgraph))
            if names != self._slot_names[name] or (changed and any(n in changed for n in names)):
                self._material_dirty.add(name)
        if changed:
            # Sources of instances may live outside the scene.
            for obj in bpy.data.objects:
                if obj.type in SUPPORTED_TYPES and any(
                    s.material is not None and s.material.name_full in changed for s in obj.material_slots
                ):
                    self._material_dirty.add(obj.name_full)
        palette_key = _key(palette)
        if palette_key != self._palette_key:
            self._palette_key = palette_key
            self._palette = palette
            tail.append(_json_frame({"type": "materials", "materials": palette}))

    def _settle_painted_images(self, now):
        """Once painting pauses, resends the changed images to devices."""
        settled = [name for name, t in self._painted.items() if now - t >= IMAGE_SETTLE]
        if not settled:
            return
        for name in settled:
            del self._painted[name]
            # Same ids, new pixels: only the textures go out again, not meshes.
            for tid in self.textures.pixels_changed(name):
                for client in self.clients:
                    client.sent_textures.discard(tid)

    def _scene_materials(self):
        """Every material in the file that something uses, for trying on in
        the app, and every one's info as a key, by full name."""
        out = []
        keys = {}
        for mat in bpy.data.materials:
            if mat.users == 0 or getattr(mat, "is_grease_pencil", False) or mat.name.startswith(_SCAN_PREFIX):
                continue
            # Every used material registers its textures, so none are
            # forgotten, but only the first ones are offered in the app.
            info = material_info(mat)
            keys[mat.name_full] = _key(info)
            if len(out) < MAX_PALETTE:
                out.append(info)
        return out, keys

    def _send_textures(self, clients):
        """Sends each device the textures its materials use that it doesn't have
        yet, a few megabytes per tick. At most one new texture is encoded per
        tick, since scaling a large image takes a moment."""
        wanted = [c for c in clients if textures.FEATURE in c.features]
        if not wanted:
            return
        encoded_one = False
        for client in wanted:
            budget = TEXTURE_BYTES_PER_TICK
            for tid in self.textures.ids():
                if tid in client.sent_textures:
                    continue
                # Wait for the device to catch up, so the queue never
                # overflows and drops what's already on its way.
                if client.pending_bytes > TEXTURE_BACKLOG_LIMIT:
                    break
                if not self.textures.ready(tid):
                    if encoded_one:
                        break
                    encoded_one = True
                frame = self.textures.frame(tid)
                if frame is not None:
                    client.send(frame)
                    budget -= len(frame)
                client.sent_textures.add(tid)
                if budget <= 0:
                    break

    # ------------------------------------------------------------ snapshots

    def _run_snapshot_job(self, scene, view_layer, accept, clients):
        """Exports at most one queued snapshot per tick and starts sending it."""
        if not self._snapshot_jobs:
            return
        job = self._snapshot_jobs.pop(0)
        able = [c for c in clients if CLIENT_SNAPSHOT in c.features and c.alive and c.approval == "allowed"]
        targets = [job["client"]] if job["client"] is not None else able
        targets = [c for c in targets if c in able]
        if not targets:
            return
        # The export selects objects and evaluates the scene again. Those
        # updates aren't edits, so they mustn't resend meshes to every device.
        self._exporting = True
        try:
            data = snapshot.export_usdz(scene, view_layer, accept)
            view_layer.update()
        except snapshot.SnapshotError as exc:
            print(f"ProjectAR: snapshot not sent ({exc.reason}): {exc}")
            frame = _json_frame({"type": "snapshotFailed", "id": job["id"], "reason": exc.reason})
            for client in targets:
                client.send(frame)
            return
        finally:
            self._exporting = False
        for client in targets:
            self._snapshot_sends.append({"client": client, "id": job["id"], "name": scene.name, "data": data, "index": 0})

    def _send_snapshots(self):
        """Queues the next chunks of snapshots on their way, a few megabytes
        per device per tick. If a device's backlog was dropped, its file
        starts again from chunk 0."""
        still = []
        for send in self._snapshot_sends:
            client = send["client"]
            if not client.alive or client.approval != "allowed":
                continue
            if client.needs_full_sync and send["index"] > 0:
                send["index"] = 0  # queued chunks may have been dropped
            count = snapshot.chunk_count(len(send["data"]))
            budget = SNAPSHOT_BYTES_PER_TICK
            while send["index"] < count and budget > 0 and client.pending_bytes <= TEXTURE_BACKLOG_LIMIT:
                frame = snapshot.chunk_frame(send["id"], send["name"], send["data"], send["index"])
                client.send(frame)
                budget -= len(frame)
                send["index"] += 1
            if send["index"] < count:
                still.append(send)
        self._snapshot_sends = still

    # internals
    def _handle_messages(self, scene, view_layer):
        """Applies waiting messages, oldest first, until the time budget is
        used up. The rest wait for the next tick."""
        changed_any = False
        deadline = time.monotonic() + MESSAGE_BUDGET
        while self._inbox and time.monotonic() < deadline:
            client, message = self._inbox.popleft()
            kind = message.get("type")
            if kind == "client":
                # The app says what it can show, such as textures, and who it is.
                features = message.get("features")
                if isinstance(features, list):
                    new = {str(f) for f in features[:32]}
                    if client.approval == "allowed" and client.identified and new != client.features:
                        client.needs_full_sync = True  # what it gets depends on these
                    client.features = new
                client.identified = True
                device_id = message.get("deviceID")
                if isinstance(device_id, str) and device_id.strip() and client.approval == "new":
                    client.device_id = review._one_line(device_id, 64)
                    name = message.get("deviceName")
                    client.device_name = review._one_line(name, 60) if isinstance(name, str) else None
                continue
            if client.approval != "allowed":
                continue  # nothing from a device Blender hasn't allowed
            if kind == "requestSnapshot":
                # One at a time per device, so a stuck app can't queue export after export.
                if CLIENT_SNAPSHOT in client.features and not self._snapshot_pending_for(client):
                    self.request_snapshot(_clean_id(message.get("id")) or None, client)
                continue
            try:
                changed = review.handle_message(message, scene, view_layer, self._space)
            except Exception as exc:  # one bad message shouldn't stop the rest
                print(f"ProjectAR: could not apply {kind} from {client.label}: {exc}")
                continue
            changed_any = changed_any or bool(changed)
            if changed:
                study.log_app_message(message)
        if changed_any:
            _push_undo()

    def _update_approvals(self, clients, opts, now):
        """Decides which new devices may have the scene, and asks in Blender
        about the rest. A device waiting gets only "approval" messages and
        pings, not even the scene's name."""
        self._denied = {k: t for k, t in self._denied.items() if t > now}
        for client in clients:
            if client.approval == "pending" and not opts.require_approval:
                # Asking was turned off while this device waited.
                client.approval = "allowed"
                client.needs_full_sync = True
                _send_approval(client, "allowed")
                continue
            if client.approval == "new":
                # The app says who it is and what it reads right after
                # connecting, which decides what it's sent. Older apps don't.
                if not client.identified and now - client.connected_at < IDENTIFY_WAIT:
                    continue
                if not opts.require_approval:
                    client.approval = "allowed"
                    continue
                key = client.device_key
                if key in opts.trusted:
                    client.approval = "allowed"
                    _send_approval(client, "allowed")
                elif key in self._denied:
                    _send_approval(client, "denied")
                    client.finish()
                elif sum(c.approval == "pending" for c in clients) >= MAX_REQUESTS:
                    # Requests can't crowd out devices Blender knows.
                    _send_approval(client, "busy")
                    client.finish()
                else:
                    client.approval = "pending"
                    client.pending_since = now
                    client.last_ping = now
                    _send_approval(client, "waiting")
                    if self.on_request is not None:
                        self.on_request(client)
            elif client.approval == "pending":
                if now - client.pending_since > APPROVAL_TIMEOUT:
                    _send_approval(client, "expired")
                    client.finish()
                elif now - client.last_ping > PING_INTERVAL:
                    # The app gives up on a silent connection, so keep it alive while asking.
                    client.last_ping = now
                    client.send(_json_frame({"type": "ping"}), force=True)
        self.requests = [c for c in clients if c.approval == "pending" and c.alive]

    def answer(self, label, allow):
        """Allows or refuses a waiting device, and any other connection from it.
        Returns the device key, or None if it's gone."""
        client = next((c for c in self.server.live_clients() if c.label == label and c.approval == "pending"), None)
        if client is None:
            return None
        key = client.device_key
        for other in self.server.live_clients():
            if other.approval != "pending" or other.device_key != key:
                continue
            if allow:
                other.approval = "allowed"
                other.needs_full_sync = True
                _send_approval(other, "allowed")
            else:
                _send_approval(other, "denied")
                other.finish()
        if not allow:
            self._denied[key] = time.monotonic() + DENIED_COOLDOWN
        self.requests = [c for c in self.requests if c.alive and c.approval == "pending"]
        return key

    def _gather_instances(
        self, depsgraph, accept, opts, now, commit, flat, fresh_flat, proto, refresh_protos, check_slots=False
    ):
        """Collection, particle and Geometry Nodes instances.

        For apps that use instancing, each unique geometry becomes a
        prototype mesh, built once, and each instance names its prototype.
        For older apps each instance is sent as its own mesh, and meshes
        shared by several instances are only built once per tick.
        """
        self.instances_capped = False
        tick = self._proto_tick = _ProtoTick()
        out = {}
        if not opts.include_instances:
            self._drop_unused_protos(set(), commit, tick)
            self.prototype_count = 0
            return out
        cache = {}
        visited = set()
        used = set()
        for inst in depsgraph.object_instances:
            if not inst.is_instance:
                continue
            obj = inst.object
            if obj.type not in SUPPORTED_TYPES:
                continue
            parent, source = inst.parent.original, obj.original
            pid = _persistent_id(inst)
            if source == parent and len(pid) == 1:
                # Curve, text, surface and metaball objects list their own
                # evaluated mesh here too. It is already sent as the object.
                continue
            if not (accept(parent) and getattr(source, "viewar_visible", True)):
                continue
            if len(out) >= opts.max_instances:
                self.instances_capped = True
                break
            iid = f"{parent.name_full}/{source.name_full}#{'.'.join(map(str, pid))}"
            names = (parent.name_full, source.name_full)
            geometry_dirty = iid in self._dirty or iid not in self._known or any(n in self._dirty for n in names)
            material_dirty = iid in self._material_dirty or any(n in self._material_dirty for n in names)
            due = now - self._last_geometry.get(iid, 0.0) >= GEOMETRY_MIN_INTERVAL
            entry = _Instance(self._convert(iid, inst.matrix_world), source=source.name_full)
            if flat:
                # A material assigned to a source's slot isn't always reported,
                # so the slots are compared once a second.
                if check_slots and not material_dirty and iid in self._slot_names:
                    material_dirty = slot_names(obj) != self._slot_names[iid]
                stale = geometry_dirty or material_dirty
                if fresh_flat or (stale and due):
                    entry.frame = self._instance_frame(iid, obj, opts, cache)
                    entry.changed = stale
                if commit:
                    if entry.changed:
                        self._dirty.discard(iid)
                        self._material_dirty.discard(iid)
                        self._last_geometry[iid] = now
                    elif geometry_dirty and iid in self._known:
                        self._dirty.add(iid)
                    elif material_dirty:
                        self._material_dirty.add(iid)
            elif commit:
                self._dirty.discard(iid)
                self._material_dirty.discard(iid)
            if proto:
                entry.proto = self._prototype_for(
                    obj, source, names, opts, now, commit, refresh_protos, visited, tick
                )
                used.add(entry.proto)
                if not flat:
                    self._triangles[iid] = self._proto_triangles.get(entry.proto, 0)
                if commit and self._instance_proto.get(iid) != entry.proto:
                    self._instance_proto[iid] = entry.proto
                    tick.mapping.append({"id": iid, "mesh": entry.proto, "source": entry.source})
            out[iid] = entry
        self._drop_unused_protos(used, commit, tick)
        self.prototype_count = len(used)
        return out

    def _prototype_for(self, obj, source, names, opts, now, commit, refresh, visited, tick):
        """The prototype id for an instance's geometry, building or updating
        the prototype once per tick when needed."""
        data = obj.data
        key = (
            source.name_full,
            data.as_pointer() if data is not None else 0,
            data.name_full if data is not None else "",
            slot_names(obj),
        )
        pid = self._protos.get(key)
        if pid is None:
            self._proto_counter += 1
            pid = f"{PROTOTYPE_PREFIX}{self._proto_counter}"
            self._protos[key] = pid
        if pid in visited:
            return pid
        visited.add(pid)
        geometry_dirty = refresh or pid in self._dirty or any(n in self._dirty for n in names)
        material_dirty = pid in self._material_dirty or any(n in self._material_dirty for n in names)
        due = refresh or now - self._last_geometry.get(pid, 0.0) >= GEOMETRY_MIN_INTERVAL
        if pid not in self._proto_frames or (geometry_dirty and due):
            tick.built[pid] = self._prototype_frame(pid, obj, opts)
            if commit:
                self._last_geometry[pid] = now
                self._dirty.discard(pid)
                self._material_dirty.discard(pid)
        elif geometry_dirty:
            if commit:
                self._dirty.add(pid)
        elif material_dirty:
            if not due:
                if commit:
                    self._material_dirty.add(pid)
                return pid
            infos = _slot_infos(obj)
            if len(infos) != self._slot_counts.get(pid):
                tick.built[pid] = self._prototype_frame(pid, obj, opts)
            elif _key(infos) != self._sent_materials.get(pid):
                tick.materials[pid] = infos
                tick.material_frames[pid] = self._prototype_frame(pid, obj, opts)
            if commit:
                self._last_geometry[pid] = now
                self._material_dirty.discard(pid)
        return pid

    def _drop_unused_protos(self, used, commit, tick):
        if not commit:
            return
        for pid in [p for p in self._proto_frames if p not in used]:
            self._proto_frames.pop(pid, None)
            self._proto_triangles.pop(pid, None)
            self._sent_materials.pop(pid, None)
            self._slot_counts.pop(pid, None)
            self._last_geometry.pop(pid, None)
            self._dirty.discard(pid)
            self._material_dirty.discard(pid)
            tick.removed.append(pid)
            tick.built.pop(pid, None)
        self._protos = {k: p for k, p in self._protos.items() if p in used}

    def _convert(self, name, world):
        """World matrix in RealityKit space, cached because rounding 16
        floats for every object on every tick dominates idle ticks."""
        cached = self._world_cache.get(name)
        if cached is not None and cached[0] == world:
            return cached[1]
        flat = _flatten(self._space @ world @ AXIS_INV @ self._unscale)
        self._world_cache[name] = (world.copy(), flat)
        return flat

    def _instance_frame(self, iid, obj, opts, cache):
        key = (
            obj.data.as_pointer() if obj.data is not None else 0,
            tuple(s.material.name_full if s.material else "" for s in obj.material_slots),
        )
        geometry = cache.get(key)
        if geometry is None:
            geometry = cache[key] = read_geometry(obj, opts.max_triangles, self._unit)
        self._record(iid, geometry)
        self._slot_names[iid] = key[1]
        return encode_mesh(iid, geometry)

    def _prototype_frame(self, pid, obj, opts):
        geometry = read_geometry(obj, opts.max_triangles, self._unit)
        self._proto_triangles[pid] = 0 if geometry.skipped else geometry.triangles
        self._slot_counts[pid] = len(geometry.materials)
        self._sent_materials[pid] = _key(geometry.materials)
        frame = encode_mesh(pid, geometry, prototype=True)
        self._proto_frames[pid] = frame
        return frame

    def _object_frame(self, name, obj, depsgraph, opts):
        source = _evaluated(obj, depsgraph)
        geometry = read_geometry(source, opts.max_triangles, self._unit)
        self._record(name, geometry)
        self._slot_counts[name] = len(geometry.materials)
        self._sent_materials[name] = _key(geometry.materials)
        self._slot_names[name] = slot_names(source)
        return encode_mesh(name, geometry)

    def _record(self, name, geometry):
        self._triangles[name] = 0 if geometry.skipped else geometry.triangles
        if geometry.skipped:
            self.skipped.add(name)
        else:
            self.skipped.discard(name)

    def _full_sync(self, client, scene, depsgraph, opts, objects, instances, matrices, built):
        client.needs_full_sync = False
        # The app drops its textures on a new scene, so send them all again.
        client.sent_textures.clear()
        client.send(
            _json_frame(
                {
                    "type": "hello",
                    "version": PROTOCOL_VERSION,
                    "scene": scene.name,
                    "unitScale": 1.0,  # everything is sent in meters
                    "features": FEATURES,
                }
            ),
            force=True,
        )
        client.send(_json_frame({"type": "clear"}), force=True)
        for name, obj in objects.items():
            if name not in built:  # shared between devices joining together
                built[name] = self._object_frame(name, obj, depsgraph, opts)
            client.send(built[name], force=True)
        if CLIENT_INSTANCING in client.features:
            sent = set()
            for entry in instances.values():
                if entry.proto not in sent and entry.proto in self._proto_frames:
                    sent.add(entry.proto)
                    client.send(self._proto_frames[entry.proto], force=True)
            if instances:
                mapping = [{"id": i, "mesh": e.proto, "source": e.source} for i, e in instances.items()]
                client.send(_json_frame({"type": "instances", "instances": mapping}), force=True)
        else:
            for entry in instances.values():
                if entry.frame is not None:
                    client.send(entry.frame, force=True)
        transforms = [{"id": n, "m": m} for n, m in matrices.items()]
        client.send(_json_frame({"type": "transforms", "objects": transforms}), force=True)

    def _forget(self, name):
        self._dirty.discard(name)
        self._material_dirty.discard(name)
        self._sent_matrices.pop(name, None)
        self._world_cache.pop(name, None)
        self._last_geometry.pop(name, None)
        self._triangles.pop(name, None)
        self._sent_materials.pop(name, None)
        self._slot_counts.pop(name, None)
        self._slot_names.pop(name, None)
        self._instance_proto.pop(name, None)
        self.skipped.discard(name)
