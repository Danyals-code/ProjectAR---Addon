# SPDX-License-Identifier: GPL-3.0-or-later
"""Tracks scene changes and turns them into ProjectAR protocol frames."""

import collections
import json
import math
import struct
import time
from dataclasses import dataclass, field

import bpy
import numpy as np
from mathutils import Matrix

from . import review, study, textures
from .server import KIND_JSON, KIND_MESH, encode_frame

PROTOCOL_VERSION = 1
# Optional extras listed in "hello". The app only uses what's listed here.
FEATURES = ["setMaterial", "variants", "notes", "roomScan", "decisions", "textures", "pairing"]
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
PALETTE_INTERVAL = 2.0
# Painted images are resent once painting pauses for this long.
IMAGE_SETTLE = 0.75  # how often the list of scene materials is checked
MAX_PALETTE = 200
# Texture bytes queued per device per tick, so meshes and pings keep flowing.
TEXTURE_BYTES_PER_TICK = 4 * 1024 * 1024
# Textures wait while a device still has this much to receive.
TEXTURE_BACKLOG_LIMIT = 8 * 1024 * 1024
# Materials the add-on makes for room scans aren't design materials.
_SCAN_PREFIX = "ProjectAR Scan "

# Set by the streamer. Materials list their textures here.
_texture_store = None

# Depsgraph pads unused levels of ObjectInstance.persistent_id with this.
UNUSED_PERSISTENT_ID = 2**31 - 1

# Blender (x, y, z) -> RealityKit (x, z, -y). See protocol section 4.4.
AXIS = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))
AXIS_INV = AXIS.inverted()

DEFAULT_MATERIAL = {
    "name": "Default",
    "color": [0.8, 0.8, 0.8, 1.0],
    "metallic": 0.0,
    "roughness": 0.5,
}

# Blender 5.0 deprecated Material.use_nodes: node materials are always used.
_HAS_USE_NODES = bpy.app.version < (5, 0, 0)


@dataclass
class Options:
    """What to stream and how. Read from the add-on settings every tick."""

    max_triangles: int = 300_000
    include_instances: bool = True
    max_instances: int = 500
    scope: str = "VISIBLE"  # VISIBLE, SELECTED or COLLECTION
    collection: str = ""
    origin: str = "WORLD"  # WORLD or CURSOR
    paused: bool = False
    max_texture_size: int = 1024
    # Ask in Blender before a new device gets the scene or can change it.
    require_approval: bool = True
    # Device keys allowed before, so they aren't asked again.
    trusted: frozenset = frozenset()


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
    frame: bytes = None  # mesh frame, when one was built this tick
    changed: bool = False  # True if existing devices need the frame


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


def _hidden_in(obj, view_layer):
    """Hidden with H (the eye), or not in the view layer at all."""
    try:
        return obj.hide_get(view_layer=view_layer)
    except RuntimeError:
        return True


# Time spent on messages from the app per tick, so a flood can't freeze Blender.
MESSAGE_BUDGET = 0.05
MAX_WAITING_MESSAGES = 500


def _to_srgb(c):
    c = min(max(float(c), 0.0), 1.0)
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _flatten(m):
    return [round(m[row][col], 6) for col in range(4) for row in range(4)]


def _to_realitykit(points):
    out = points[:, [0, 2, 1]]
    out[:, 2] *= -1.0
    return out


def _socket_value(node, name, fallback):
    sock = node.inputs.get(name)
    if sock is None or sock.is_linked:
        return fallback
    return sock.default_value


def _persistent_id(instance):
    pid = tuple(instance.persistent_id)
    if UNUSED_PERSISTENT_ID in pid:
        pid = pid[: pid.index(UNUSED_PERSISTENT_ID)]
    return pid


def material_info(mat):
    if mat is None:
        return dict(DEFAULT_MATERIAL)
    color = list(mat.diffuse_color)
    metallic = float(mat.metallic)
    roughness = float(mat.roughness)
    maps = None
    tree = mat.node_tree
    if tree is not None and (mat.use_nodes if _HAS_USE_NODES else True):
        principled = next(
            (n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None
        )
        if principled is not None:
            color = list(_socket_value(principled, "Base Color", color))
            metallic = float(_socket_value(principled, "Metallic", metallic))
            roughness = float(_socket_value(principled, "Roughness", roughness))
            color[3] = float(_socket_value(principled, "Alpha", color[3]))
            if _texture_store is not None:
                found, alpha = textures.material_textures(principled)
                if found:
                    maps = {role: _texture_store.register(image, role) for role, image in found.items()}
                    if alpha:
                        maps["alpha"] = True
    info = {
        "name": mat.name,
        "color": [_to_srgb(c) for c in color[:3]] + [min(max(color[3], 0.0), 1.0)],
        "metallic": metallic,
        "roughness": roughness,
    }
    if maps:
        # Older apps ignore this and keep the plain color.
        info["textures"] = maps
    return info


def read_geometry(obj_eval, max_triangles):
    """Triangulate an evaluated object (or instance) into protocol blobs."""
    materials = [material_info(s.material) for s in obj_eval.material_slots]
    geometry = Geometry(materials=materials or [dict(DEFAULT_MATERIAL)])
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


def encode_mesh(object_id, geometry):
    header = {
        "id": object_id,
        "vertexCount": geometry.vertex_count,
        "indexCount": geometry.index_count,
        "hasUVs": geometry.has_uvs,
        "materials": geometry.materials,
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
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


# ---------------------------------------------------------------- streamer


class SceneStreamer:
    def __init__(self, server, options):
        self.server = server
        self._options = options  # callable returning Options, read every tick
        self._dirty = set()
        self._known = set()
        self._sent_matrices = {}
        self._world_cache = {}  # id -> (world matrix, converted matrix)
        self._space = None
        self._inbox = collections.deque()  # (client, message) waiting to be applied
        self._last_geometry = {}
        self._triangles = {}
        self._last_ping = 0.0
        self._budget = None
        self._sync_key = None
        self._variants_key = None
        self._notes_key = None
        self._palette = []
        self._palette_key = None
        self._last_palette = 0.0
        self._painted = {}  # image name -> when its pixels last changed
        global _texture_store
        self.textures = _texture_store = textures.TextureStore(1024)
        self.skipped = set()
        self.clients = []
        # Devices waiting to be allowed, newest last, and ones refused recently.
        self.requests = []
        self._denied = {}  # device key -> time it may ask again
        self.on_request = None  # called with a client when Blender should ask
        self.object_count = 0
        self.instance_count = 0
        self.instances_capped = False

    # stats for the UI
    @property
    def client_labels(self):
        return [c.label for c in self.clients]

    @property
    def triangle_count(self):
        return sum(self._triangles.values())

    def request_full_sync(self):
        for client in self.server.live_clients():
            client.needs_full_sync = True

    # called from depsgraph_update_post (main thread)
    def on_depsgraph_update(self, scene, depsgraph):
        materials, datas = set(), set()
        for update in depsgraph.updates:
            block = getattr(update.id, "original", update.id)
            if isinstance(block, bpy.types.Object):
                if update.is_updated_geometry:
                    self._dirty.add(block.name_full)
            elif isinstance(block, bpy.types.Material):
                materials.add(block.name_full)
            elif isinstance(block, bpy.types.Image):
                self._painted[block.name_full] = time.monotonic()
            elif isinstance(block, bpy.types.ShaderNodeTree):
                # Shader Editor edits can report the embedded node tree
                # instead of the material that owns it.
                for mat in bpy.data.materials:
                    if mat.node_tree is not None and mat.node_tree == block:
                        materials.add(mat.name_full)
            elif update.is_updated_geometry and isinstance(
                block, (bpy.types.Mesh, bpy.types.Curve, bpy.types.MetaBall)
            ):
                datas.add(block.name_full)

        if not (materials or datas):
            return
        # All objects, not just the scene's: instanced collections often
        # live outside the scene.
        for obj in bpy.data.objects:
            if obj.type not in SUPPORTED_TYPES:
                continue
            if obj.data is not None and obj.data.name_full in datas:
                self._dirty.add(obj.name_full)
            elif materials and any(
                s.material is not None and s.material.name_full in materials
                for s in obj.material_slots
            ):
                self._dirty.add(obj.name_full)

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
            return

        opts = self._options()
        context = bpy.context
        scene = context.scene
        view_layer = context.view_layer or scene.view_layers[0]

        space = AXIS
        if opts.origin == "CURSOR":
            space = AXIS @ scene.cursor.matrix.inverted_safe()
        if space != self._space:
            self._space = space
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
        self._sync_key = sync_key
        # Objects skipped under the old triangle budget may fit now, and vice versa.
        if self._budget is not None and opts.max_triangles != self._budget:
            self._dirty |= self._known
        self._budget = opts.max_triangles

        existing = [c for c in clients if not c.needs_full_sync]
        fresh = [c for c in clients if c.needs_full_sync]
        frames = []
        if now - self._last_ping > PING_INTERVAL:
            self._last_ping = now
            frames.append(_json_frame({"type": "ping"}))

        if opts.paused and not fresh:
            for client in existing:
                for frame in frames:
                    client.send(frame)
            return

        # While paused, new devices still get the current scene, but state is
        # left alone so existing devices catch up on everything after resuming.
        commit = not opts.paused
        accept = _scope_filter(view_layer, opts)
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
        instances = self._gather_instances(
            depsgraph, accept, opts, now, full=bool(fresh), commit=commit
        )
        self.object_count = len(objects)
        self.instance_count = len(instances)
        matrices = {n: self._convert(n, o.matrix_world) for n, o in objects.items()}
        matrices.update((i, entry.matrix) for i, entry in instances.items())
        built = {}

        if commit:
            current = matrices.keys()
            removed = self._known - current
            added = current - self._known
            self._known = set(current)
            for name in removed:
                self._forget(name)
            if removed:
                frames.append(_json_frame({"type": "remove", "ids": sorted(removed)}))

            self._dirty |= added & objects.keys()
            for name in list(self._dirty):
                if name in instances:
                    continue  # rate limited instance, retried by _gather_instances
                obj = objects.get(name)
                if obj is None:
                    self._dirty.discard(name)
                    continue
                if now - self._last_geometry.get(name, 0.0) < GEOMETRY_MIN_INTERVAL:
                    continue  # rate limited, try again next tick
                self._dirty.discard(name)
                self._last_geometry[name] = now
                if existing:  # fresh clients get everything in their full sync
                    built[name] = self._object_frame(name, obj, depsgraph, opts)
                    frames.append(built[name])
            frames.extend(e.frame for e in instances.values() if e.changed)

            changed = []
            for name, matrix in matrices.items():
                if self._sent_matrices.get(name) != matrix:
                    self._sent_matrices[name] = matrix
                    changed.append({"id": name, "m": matrix})
            if changed:
                frames.append(_json_frame({"type": "transforms", "objects": changed}))

        # While paused, existing devices stay as they are. The keys stay
        # old, so they get these changes once updates resume.
        variants_key = json.dumps(variants, sort_keys=True)
        if commit and variants_key != self._variants_key:
            self._variants_key = variants_key
            frames.append(_json_frame({"type": "variants", "sets": variants}))
        notes_key = json.dumps(notes, sort_keys=True)
        if commit and notes_key != self._notes_key:
            self._notes_key = notes_key
            frames.append(_json_frame({"type": "notes", "notes": notes}))
        if commit and (fresh or now - self._last_palette > PALETTE_INTERVAL):
            self._last_palette = now
            # Every material in use registers its textures again here, so
            # ids no longer used (old versions, other files) stop being sent.
            self.textures.begin_epoch()
            palette = self._scene_materials()
            palette_key = json.dumps(palette, sort_keys=True)
            if palette_key != self._palette_key:
                self._palette_key = palette_key
                self._palette = palette
                frames.append(_json_frame({"type": "materials", "materials": palette}))

        for client in existing:
            for frame in frames:
                client.send(frame)
        for client in fresh:
            self._full_sync(client, scene, depsgraph, opts, objects, instances, matrices, built)
            client.send(_json_frame({"type": "variants", "sets": variants}), force=True)
            client.send(_json_frame({"type": "notes", "notes": notes}), force=True)
            client.send(_json_frame({"type": "materials", "materials": self._palette}), force=True)
        self._send_textures(clients)

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
        """Every material in the file that something uses, for trying on in the app."""
        out = []
        for mat in bpy.data.materials:
            if mat.users == 0 or getattr(mat, "is_grease_pencil", False) or mat.name.startswith(_SCAN_PREFIX):
                continue
            # Every used material registers its textures, so none are
            # forgotten, but only the first ones are offered in the app.
            info = material_info(mat)
            if len(out) < MAX_PALETTE:
                out.append(info)
        return out

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

    # internals
    def _handle_messages(self, scene, view_layer):
        """Applies waiting messages, oldest first, until the time budget is
        used up. The rest wait for the next tick."""
        changed_any = False
        deadline = time.monotonic() + MESSAGE_BUDGET
        while self._inbox and time.monotonic() < deadline:
            client, message = self._inbox.popleft()
            if message.get("type") == "client":
                # The app says what it can show, such as textures, and who it is.
                features = message.get("features")
                if isinstance(features, list):
                    client.features = {str(f) for f in features[:32]}
                device_id = message.get("deviceID")
                if isinstance(device_id, str) and device_id.strip() and client.approval == "new":
                    client.device_id = review._one_line(device_id, 64)
                    name = message.get("deviceName")
                    client.device_name = review._one_line(name, 60) if isinstance(name, str) else None
                continue
            if client.approval != "allowed":
                continue  # nothing from a device Blender hasn't allowed
            try:
                changed = review.handle_message(message, scene, view_layer, self._space)
            except Exception as exc:  # one bad message shouldn't stop the rest
                print(f"ProjectAR: could not apply {message.get('type')} from {client.label}: {exc}")
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
                if not opts.require_approval:
                    client.approval = "allowed"
                    continue
                # The app says who it is right after connecting. Older apps don't.
                if client.device_id is None and now - client.connected_at < IDENTIFY_WAIT:
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

    def _gather_instances(self, depsgraph, accept, opts, now, full, commit):
        """Collection, particle and Geometry Nodes instances, flattened.

        Protocol v1 has no instancing, so each instance is sent as its own
        object. Meshes shared by several instances are only built once.
        """
        self.instances_capped = False
        if not opts.include_instances:
            return {}
        out = {}
        cache = {}
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
            dirty = (
                iid in self._dirty
                or iid not in self._known
                or parent.name_full in self._dirty
                or source.name_full in self._dirty
            )
            due = now - self._last_geometry.get(iid, 0.0) >= GEOMETRY_MIN_INTERVAL
            entry = _Instance(self._convert(iid, inst.matrix_world))
            if full or (dirty and due):
                entry.frame = self._instance_frame(iid, obj, opts, cache)
                entry.changed = dirty
            if commit:
                if entry.changed:
                    self._dirty.discard(iid)
                    self._last_geometry[iid] = now
                elif dirty:
                    self._dirty.add(iid)
            out[iid] = entry
        return out

    def _convert(self, name, world):
        """World matrix in RealityKit space, cached because rounding 16
        floats for every object on every tick dominates idle ticks."""
        cached = self._world_cache.get(name)
        if cached is not None and cached[0] == world:
            return cached[1]
        flat = _flatten(self._space @ world @ AXIS_INV)
        self._world_cache[name] = (world.copy(), flat)
        return flat

    def _instance_frame(self, iid, obj, opts, cache):
        key = (
            obj.data.as_pointer() if obj.data is not None else 0,
            tuple(s.material.name_full if s.material else "" for s in obj.material_slots),
        )
        geometry = cache.get(key)
        if geometry is None:
            geometry = cache[key] = read_geometry(obj, opts.max_triangles)
        self._record(iid, geometry)
        return encode_mesh(iid, geometry)

    def _object_frame(self, name, obj, depsgraph, opts):
        # Hidden variant options in excluded collections aren't evaluated.
        try:
            source = obj.evaluated_get(depsgraph)
        except (RuntimeError, ReferenceError):
            source = obj
        geometry = read_geometry(source, opts.max_triangles)
        self._record(name, geometry)
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
                    "unitScale": round(scene.unit_settings.scale_length, 6),
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
        for entry in instances.values():
            client.send(entry.frame, force=True)
        transforms = [{"id": n, "m": m} for n, m in matrices.items()]
        client.send(_json_frame({"type": "transforms", "objects": transforms}), force=True)

    def _forget(self, name):
        self._dirty.discard(name)
        self._sent_matrices.pop(name, None)
        self._world_cache.pop(name, None)
        self._last_geometry.pop(name, None)
        self._triangles.pop(name, None)
        self.skipped.discard(name)
