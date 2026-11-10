# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns a host's scene into ProjectAR frames for each device.

The host (rhino_host for Rhino) reports the scene every tick as a Scene:
objects with a version that changes when their mesh must be sent again, a
material key that changes when only their materials did, and their matrices;
block instances with the prototype geometry they share; and the review data.
The streamer works out what devices are missing and sends only that, the way
the Blender add-on does:

- Apps that list "instancing" get one prototype mesh per unique instanced
  geometry plus "instances" entries, older apps one mesh per instance.
- Apps that list "materialUpdates" get "objectMaterials" when only materials
  changed, older apps the whole mesh again.
- A device that hasn't connected before waits until it's allowed, then gets
  a full sync. So does one that falls behind, or when the document changes.

Nothing here touches Rhino, so it is tested with a fake host and a fake app.
"""

import collections
import time
import uuid
from dataclasses import dataclass, field

from . import snapshot, textures
from .protocol import (
    CLIENT_INSTANCING,
    CLIENT_MATERIAL_UPDATES,
    CLIENT_SNAPSHOT,
    FEATURES,
    HOST,
    PROTOCOL_VERSION,
    PROTOTYPE_PREFIX,
    clean_id,
    encode_mesh,
    json_frame,
    key,
    one_line,
)

GEOMETRY_MIN_INTERVAL = 0.1  # max 10 mesh resends per second per object
PING_INTERVAL = 2.0
# How long a device waits for its "client" message before it's known by its IP.
IDENTIFY_WAIT = 2.0
# Unanswered requests to connect are turned away after this long.
APPROVAL_TIMEOUT = 120.0
# A refused device is refused again without asking for this long.
DENIED_COOLDOWN = 60.0
MAX_REQUESTS = 3
# Texture bytes queued per device per tick, so meshes and pings keep flowing.
TEXTURE_BYTES_PER_TICK = 4 * 1024 * 1024
# Textures wait while a device still has this much to receive.
TEXTURE_BACKLOG_LIMIT = 8 * 1024 * 1024
# Snapshot chunks are paced the same way.
SNAPSHOT_BYTES_PER_TICK = 4 * 1024 * 1024
# Time spent on messages from the app per tick, so a flood can't freeze Rhino.
MESSAGE_BUDGET = 0.05
MAX_WAITING_MESSAGES = 500

_UNSENT = object()


@dataclass
class Options:
    """What to stream and how. Read from the settings every tick."""

    max_triangles: int = 300_000
    include_instances: bool = True
    max_instances: int = 2000
    scope: str = "VISIBLE"  # VISIBLE, SELECTED or LAYER
    layer: str = ""  # the layer streamed with LAYER, by full path
    origin: str = "WORLD"  # WORLD or POINT
    paused: bool = False
    max_texture_size: int = 1024
    # Ask in Rhino before a new device gets the scene or can change it.
    require_approval: bool = True
    # Device keys allowed before, so they aren't asked again.
    trusted: frozenset = frozenset()
    stream_lights: bool = True


@dataclass
class SceneObject:
    """An object as devices see it. version changes when the mesh must be
    sent again; material_key when only its materials changed."""

    version: object
    matrix: list  # 16 floats, column-major, RealityKit space
    material_key: str
    materials: object  # callable() -> [material info]
    build: object  # callable(max_triangles) -> protocol.Geometry


@dataclass
class SceneInstance:
    """One placed copy of a mesh. Instances with the same proto_key share
    one prototype mesh on apps that use instancing."""

    proto_key: object
    matrix: list
    source: str  # the object a material change from the app goes to
    material_key: str
    materials: object
    build: object


@dataclass
class Scene:
    name: str = "Untitled"
    unit_scale: float = 1.0
    objects: dict = field(default_factory=dict)  # id -> SceneObject
    instances: dict = field(default_factory=dict)  # id -> SceneInstance
    variants: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    lights: list = field(default_factory=list)
    cameras: list = field(default_factory=list)
    palette: list = field(default_factory=list)  # materials offered for trying on
    instances_capped: bool = False


@dataclass
class _Proto:
    frame: bytes
    material_key: str
    slots: int


def _send_approval(client, state):
    client.send(json_frame({"type": "approval", "state": state}), force=True)


class Streamer:
    def __init__(self, server, host, options):
        self.server = server
        self.host = host
        self._options = options  # callable returning Options, read every tick
        self._inbox = collections.deque()  # (client, message) waiting to be applied
        self._last_ping = 0.0
        self._budget = None
        self._sync_key = None
        # What devices that are in sync have, shared by all of them.
        self._known = set()
        self._sent_version = {}
        self._sent_material = {}
        self._slot_count = {}
        self._sent_matrix = {}
        self._last_geometry = {}
        self._keys = {}  # "variants", "notes", ... -> key of what was sent
        self._flat_sent = {}  # instance id -> (proto key, material key), for older apps
        # Prototypes: geometry key -> id, id -> latest frame, and what apps have.
        self._protos = {}
        self._proto_counter = 0
        self._proto_cache = {}
        self._proto_sent = {}  # id -> (material key, slot count)
        self._instance_proto = {}  # instance id -> prototype id
        # Statistics.
        self._triangles = {}
        self._skipped_objects = set()
        self._key_triangles = {}
        self._skipped_keys = set()
        self.object_count = 0
        self.instance_count = 0
        self.prototype_count = 0
        self.instances_capped = False
        self.triangle_count = 0
        self.skipped = []
        # Snapshots: exports to run, and files on their way to devices.
        self._snapshot_jobs = []
        self._snapshot_sends = []
        self.clients = []
        # Devices waiting to be allowed, newest last, and ones refused recently.
        self.requests = []
        self._denied = {}  # device key -> time it may ask again
        self.on_request = None  # called with a client when Rhino should ask

    # ------------------------------------------------------------ for the UI

    @property
    def snapshot_clients(self):
        return [c for c in self.clients if c.approval == "allowed" and CLIENT_SNAPSHOT in c.features]

    @property
    def snapshot_busy(self):
        return bool(self._snapshot_jobs or self._snapshot_sends)

    def request_full_sync(self):
        for client in self.server.live_clients():
            client.needs_full_sync = True

    def _snapshot_pending_for(self, client):
        return any(j["client"] is client for j in self._snapshot_jobs) or any(
            s["client"] is client for s in self._snapshot_sends
        )

    def request_snapshot(self, snapshot_id=None, client=None):
        """Queues a USDZ snapshot for one device, or every device that can take one."""
        self._snapshot_jobs.append({"id": snapshot_id or str(uuid.uuid4()).upper(), "client": client})

    # ------------------------------------------------------------ tick

    def tick(self):
        """Run on Rhino's main thread, about 30 times a second."""
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
        self._handle_messages()
        self._update_approvals(clients, opts, now)
        # Only allowed devices get the scene.
        clients = [c for c in clients if c.approval == "allowed"]
        if not clients:
            return
        store = self.host.textures
        if opts.max_texture_size != store.max_size:
            store.set_max_size(opts.max_texture_size)
            for client in clients:
                client.sent_textures.clear()

        scene = self.host.scene(opts)
        if self.host.take_sync_request():  # another document, or the same one reopened
            for client in clients:
                client.needs_full_sync = True
        # "hello" carries the document's name and unit scale, so resend it on change.
        sync_key = (scene.name, round(float(scene.unit_scale), 12))
        if self._sync_key is not None and sync_key != self._sync_key:
            for client in clients:
                client.needs_full_sync = True
        self._sync_key = sync_key
        # Objects skipped under the old triangle budget may fit now, and vice versa.
        if self._budget is not None and opts.max_triangles != self._budget:
            self._sent_version.clear()
            self._flat_sent.clear()
            self._protos.clear()  # new prototypes; the old ones go as unused
            self._proto_cache.clear()
        self._budget = opts.max_triangles

        existing = [c for c in clients if not c.needs_full_sync]
        fresh = [c for c in clients if c.needs_full_sync]
        head = []
        if now - self._last_ping > PING_INTERVAL:
            self._last_ping = now
            head.append(json_frame({"type": "ping"}))

        if opts.paused and not fresh:
            for client in existing:
                for frame in head:
                    client.send(frame)
        else:
            # While paused, new devices still get the current scene, but state is
            # left alone so existing devices catch up on everything after resuming.
            commit = not opts.paused
            built = {}
            if commit and existing:
                # A device joining gets everything now, so devices already here
                # do too, or the shared state would say they have it.
                self._update(scene, existing, head, now, built, force=bool(fresh))
            for client in fresh:
                self._full_sync(client, scene, built, commit)
            self._send_textures(clients)
        self._update_stats(scene)
        self._run_snapshot_job(scene, opts, clients)
        self._send_snapshots()

    # ------------------------------------------------------------ updates

    def _update(self, scene, existing, head, now, built, force):
        current = set(scene.objects) | set(scene.instances)
        removed = self._known - current
        added = current - self._known
        self._known = current
        for name in removed:
            self._forget(name)
        for name in added:
            self._forget(name)  # anything remembered from before isn't on the devices
        if removed:
            head.append(json_frame({"type": "remove", "ids": sorted(removed)}))

        legacy = any(CLIENT_MATERIAL_UPDATES not in c.features for c in existing)
        object_frames, material_entries, material_meshes = [], [], []
        for oid, obj in scene.objects.items():
            due = force or now - self._last_geometry.get(oid, 0.0) >= GEOMETRY_MIN_INTERVAL
            if self._sent_version.get(oid, _UNSENT) != obj.version:
                if due:
                    object_frames.append(self._object_frame(oid, obj, built))
                    self._last_geometry[oid] = now
                continue  # rate limited, tried again next tick
            if obj.material_key == self._sent_material.get(oid) or not due:
                continue
            infos = obj.materials()
            self._last_geometry[oid] = now
            if len(infos) != self._slot_count.get(oid):
                object_frames.append(self._object_frame(oid, obj, built))
                continue
            self._sent_material[oid] = obj.material_key
            material_entries.append({"id": oid, "materials": infos})
            if legacy:
                material_meshes.append(self._object_frame(oid, obj, built))

        inst_clients = [c for c in existing if CLIENT_INSTANCING in c.features]
        flat_clients = [c for c in existing if CLIENT_INSTANCING not in c.features]
        proto_frames, proto_entries, proto_meshes = [], [], []
        if inst_clients:
            proto_frames, proto_entries, proto_meshes = self._update_protos(scene, inst_clients)
        flat_frames = self._update_flat(scene, now, force) if flat_clients else []

        changed = []
        for items in (scene.objects, scene.instances):
            for name, item in items.items():
                if self._sent_matrix.get(name) != item.matrix:
                    self._sent_matrix[name] = item.matrix
                    changed.append({"id": name, "m": item.matrix})
        transforms = [json_frame({"type": "transforms", "objects": changed})] if changed else []
        tail = self._changed_extras(scene)

        for client in existing:
            updates = CLIENT_MATERIAL_UPDATES in client.features
            frames = head + object_frames
            if CLIENT_INSTANCING in client.features:
                frames = frames + proto_frames
                entries = material_entries + proto_entries
                if not updates:
                    frames = frames + proto_meshes
            else:
                frames = frames + flat_frames
                entries = material_entries
            if updates:
                if entries:
                    frames = frames + [json_frame({"type": "objectMaterials", "objects": entries})]
            else:
                frames = frames + material_meshes
            for frame in frames + transforms + tail:
                client.send(frame)

    def _update_protos(self, scene, clients):
        legacy = any(CLIENT_MATERIAL_UPDATES not in c.features for c in clients)
        frames, entries, meshes, mapping = [], [], [], []
        used, visited = set(), set()
        for iid, inst in scene.instances.items():
            pid = self._pid(inst.proto_key)
            used.add(pid)
            if pid not in visited:
                visited.add(pid)
                sent = self._proto_sent.get(pid)
                if sent is None:
                    proto = self._current_proto(pid, inst)
                    frames.append(proto.frame)
                    self._proto_sent[pid] = (inst.material_key, proto.slots)
                elif sent[0] != inst.material_key:
                    infos = inst.materials()
                    if len(infos) != sent[1]:
                        proto = self._current_proto(pid, inst)
                        frames.append(proto.frame)
                        self._proto_sent[pid] = (inst.material_key, proto.slots)
                    else:
                        entries.append({"id": pid, "materials": infos})
                        if legacy:
                            meshes.append(self._current_proto(pid, inst).frame)
                        self._proto_sent[pid] = (inst.material_key, len(infos))
            if self._instance_proto.get(iid) != pid:
                self._instance_proto[iid] = pid
                mapping.append({"id": iid, "mesh": pid, "source": inst.source})
        unused = sorted(p for p in self._proto_sent if p not in used)
        for pid in unused:
            del self._proto_sent[pid]
        self._protos = {k: p for k, p in self._protos.items() if p in used}
        self._proto_cache = {p: v for p, v in self._proto_cache.items() if p in used}
        out = [json_frame({"type": "remove", "ids": unused})] if unused else []
        out.extend(frames)
        if mapping:
            out.append(json_frame({"type": "instances", "instances": mapping}))
        return out, entries, meshes

    def _update_flat(self, scene, now, force):
        frames, cache = [], {}
        for iid, inst in scene.instances.items():
            state = (inst.proto_key, inst.material_key)
            if self._flat_sent.get(iid) == state:
                continue
            if not force and now - self._last_geometry.get(iid, 0.0) < GEOMETRY_MIN_INTERVAL:
                continue
            frames.append(self._flat_frame(iid, inst, cache))
            self._flat_sent[iid] = state
            self._last_geometry[iid] = now
        return frames

    def _changed_extras(self, scene):
        """Variants, notes, the material palette, lights and cameras that
        changed since devices got them."""
        out = []
        for name, field_name, value in self._extras(scene):
            k = key(value)
            if k != self._keys.get(name):
                self._keys[name] = k
                out.append(json_frame({"type": name, field_name: value}))
        return out

    @staticmethod
    def _extras(scene):
        return (
            ("variants", "sets", scene.variants),
            ("notes", "notes", scene.notes),
            ("materials", "materials", scene.palette),
            ("lights", "lights", scene.lights),
            ("cameras", "cameras", scene.cameras),
        )

    def _full_sync(self, client, scene, built, commit):
        client.needs_full_sync = False
        # The app drops its textures on a new scene, so send them all again.
        client.sent_textures.clear()
        hello = {
            "type": "hello",
            "version": PROTOCOL_VERSION,
            "scene": scene.name,
            "unitScale": round(float(scene.unit_scale), 12),
            "features": FEATURES,
            "host": HOST,  # the app ignores this; it's for people reading traffic
        }
        client.send(json_frame(hello), force=True)
        client.send(json_frame({"type": "clear"}), force=True)
        for oid, obj in scene.objects.items():
            client.send(self._object_frame(oid, obj, built, record=commit), force=True)
        if CLIENT_INSTANCING in client.features:
            sent, mapping = set(), []
            for iid, inst in scene.instances.items():
                pid = self._pid(inst.proto_key)
                if pid not in sent:
                    sent.add(pid)
                    proto = self._current_proto(pid, inst)
                    client.send(proto.frame, force=True)
                    if commit:
                        self._proto_sent[pid] = (inst.material_key, proto.slots)
                mapping.append({"id": iid, "mesh": pid, "source": inst.source})
                if commit:
                    self._instance_proto[iid] = pid
            if mapping:
                client.send(json_frame({"type": "instances", "instances": mapping}), force=True)
        else:
            cache = {}
            for iid, inst in scene.instances.items():
                client.send(self._flat_frame(iid, inst, cache), force=True)
                if commit:
                    self._flat_sent[iid] = (inst.proto_key, inst.material_key)
        transforms = [{"id": n, "m": o.matrix} for n, o in scene.objects.items()]
        transforms += [{"id": n, "m": i.matrix} for n, i in scene.instances.items()]
        client.send(json_frame({"type": "transforms", "objects": transforms}), force=True)
        for name, field_name, value in self._extras(scene):
            client.send(json_frame({"type": name, field_name: value}), force=True)
        if commit:
            current = set(scene.objects) | set(scene.instances)
            for name in self._known - current:
                self._forget(name)
            self._known = current
            for item in transforms:
                self._sent_matrix[item["id"]] = item["m"]
            for name, _, value in self._extras(scene):
                self._keys[name] = key(value)

    # ------------------------------------------------------------ frames

    def _object_frame(self, oid, obj, built, record=True):
        entry = built.get(oid)
        if entry is None:
            geometry = obj.build(self._budget)
            entry = built[oid] = (encode_mesh(oid, geometry), len(geometry.materials))
            self._triangles[oid] = 0 if geometry.skipped else geometry.triangles
            if geometry.skipped:
                self._skipped_objects.add(oid)
            else:
                self._skipped_objects.discard(oid)
        if record:
            self._sent_version[oid] = obj.version
            self._sent_material[oid] = obj.material_key
            self._slot_count[oid] = entry[1]
        return entry[0]

    def _build_instance(self, inst):
        geometry = inst.build(self._budget)
        self._key_triangles[inst.proto_key] = 0 if geometry.skipped else geometry.triangles
        if geometry.skipped:
            self._skipped_keys.add(inst.proto_key)
        else:
            self._skipped_keys.discard(inst.proto_key)
        return geometry

    def _flat_frame(self, iid, inst, cache):
        geometry = cache.get(inst.proto_key)
        if geometry is None:
            geometry = cache[inst.proto_key] = self._build_instance(inst)
        return encode_mesh(iid, geometry)

    def _pid(self, proto_key):
        pid = self._protos.get(proto_key)
        if pid is None:
            self._proto_counter += 1
            pid = self._protos[proto_key] = f"{PROTOTYPE_PREFIX}{self._proto_counter}"
        return pid

    def _current_proto(self, pid, inst):
        """The prototype's frame with the instance's current materials,
        built when it's new or its materials changed."""
        proto = self._proto_cache.get(pid)
        if proto is None or proto.material_key != inst.material_key:
            geometry = self._build_instance(inst)
            proto = _Proto(encode_mesh(pid, geometry, prototype=True), inst.material_key, len(geometry.materials))
            self._proto_cache[pid] = proto
        return proto

    def _forget(self, name):
        for table in (self._sent_version, self._sent_material, self._slot_count, self._sent_matrix,
                      self._last_geometry, self._triangles, self._flat_sent, self._instance_proto):
            table.pop(name, None)
        self._skipped_objects.discard(name)

    def _update_stats(self, scene):
        self.object_count = len(scene.objects)
        self.instance_count = len(scene.instances)
        self.prototype_count = len({i.proto_key for i in scene.instances.values()})
        self.instances_capped = scene.instances_capped
        self.triangle_count = sum(self._triangles.get(n, 0) for n in scene.objects) + sum(
            self._key_triangles.get(i.proto_key, 0) for i in scene.instances.values()
        )
        skipped = [n for n in scene.objects if n in self._skipped_objects]
        skipped += sorted({i.source for i in scene.instances.values() if i.proto_key in self._skipped_keys})
        self.skipped = skipped
        keys = {i.proto_key for i in scene.instances.values()}
        self._key_triangles = {k: v for k, v in self._key_triangles.items() if k in keys}
        self._skipped_keys &= keys

    # ------------------------------------------------------------ textures

    def _send_textures(self, clients):
        """Sends each device the textures its materials use that it doesn't have
        yet, a few megabytes per tick. At most one new texture is encoded per
        tick, since scaling a large image takes a moment."""
        wanted = [c for c in clients if textures.FEATURE in c.features and not c.needs_full_sync]
        if not wanted:
            return
        store = self.host.textures
        encoded_one = False
        for client in wanted:
            budget = TEXTURE_BYTES_PER_TICK
            for tid in store.ids():
                if tid in client.sent_textures:
                    continue
                # Wait for the device to catch up, so the queue never
                # overflows and drops what's already on its way.
                if client.pending_bytes > TEXTURE_BACKLOG_LIMIT:
                    break
                if not store.ready(tid):
                    if encoded_one:
                        break
                    encoded_one = True
                frame = store.frame(tid)
                if frame is not None:
                    client.send(frame)
                    budget -= len(frame)
                client.sent_textures.add(tid)
                if budget <= 0:
                    break

    # ------------------------------------------------------------ snapshots

    def _run_snapshot_job(self, scene, opts, clients):
        """Exports at most one queued snapshot per tick and starts sending it."""
        if not self._snapshot_jobs:
            return
        job = self._snapshot_jobs.pop(0)
        able = [c for c in clients if CLIENT_SNAPSHOT in c.features and c.alive and c.approval == "allowed"]
        targets = [job["client"]] if job["client"] is not None else able
        targets = [c for c in targets if c in able]
        if not targets:
            return
        try:
            data = self.host.export_snapshot(scene, opts)
        except snapshot.SnapshotError as exc:
            print(f"ProjectAR: snapshot not sent ({exc.reason}): {exc}")
            frame = json_frame({"type": "snapshotFailed", "id": job["id"], "reason": exc.reason})
            for client in targets:
                client.send(frame)
            return
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

    # ------------------------------------------------------------ messages

    def _handle_messages(self):
        """Applies waiting messages, oldest first, until the time budget is
        used up. The rest wait for the next tick, as do changes the host
        can't make yet (while a Rhino command is running)."""
        began = changed_any = False
        deadline = time.monotonic() + MESSAGE_BUDGET
        try:
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
                        client.device_id = one_line(device_id, 64)
                        name = message.get("deviceName")
                        client.device_name = one_line(name, 60) if isinstance(name, str) else None
                    continue
                if client.approval != "allowed":
                    continue  # nothing from a device Rhino hasn't allowed
                if kind == "requestSnapshot":
                    # One at a time per device, so a stuck app can't queue export after export.
                    if CLIENT_SNAPSHOT in client.features and not self._snapshot_pending_for(client):
                        self.request_snapshot(clean_id(message.get("id")) or None, client)
                    continue
                if not self.host.can_edit():
                    self._inbox.appendleft((client, message))
                    break
                if not began:
                    self.host.begin_edits()
                    began = True
                try:
                    changed = self.host.apply_message(message)
                except Exception as exc:  # one bad message shouldn't stop the rest
                    print(f"ProjectAR: could not apply {kind} from {client.label}: {exc}")
                    continue
                if changed:
                    changed_any = True
                    self.host.on_app_change(message)
        finally:
            if began:
                self.host.end_edits(changed_any)

    def _update_approvals(self, clients, opts, now):
        """Decides which new devices may have the scene, and asks in Rhino
        about the rest. A device waiting gets only "approval" messages and
        pings, not even the document's name."""
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
                device = client.device_key
                if device in opts.trusted:
                    client.approval = "allowed"
                    _send_approval(client, "allowed")
                elif device in self._denied:
                    _send_approval(client, "denied")
                    client.finish()
                elif sum(c.approval == "pending" for c in clients) >= MAX_REQUESTS:
                    # Requests can't crowd out devices Rhino knows.
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
                    client.send(json_frame({"type": "ping"}), force=True)
        self.requests = [c for c in clients if c.approval == "pending" and c.alive]

    def answer(self, label, allow):
        """Allows or refuses a waiting device, and any other connection from it.
        Returns the device key, or None if it's gone."""
        client = next((c for c in self.server.live_clients() if c.label == label and c.approval == "pending"), None)
        if client is None:
            return None
        device = client.device_key
        for other in self.server.live_clients():
            if other.approval != "pending" or other.device_key != device:
                continue
            if allow:
                other.approval = "allowed"
                other.needs_full_sync = True
                _send_approval(other, "allowed")
            else:
                _send_approval(other, "denied")
                other.finish()
        if not allow:
            self._denied[device] = time.monotonic() + DENIED_COOLDOWN
        self.requests = [c for c in self.requests if c.alive and c.approval == "pending"]
        return device
