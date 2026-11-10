# SPDX-License-Identifier: GPL-3.0-or-later
"""The streamer end to end: a real server on 127.0.0.1, a fake host for
Rhino, and fake apps that decode every frame. Run from the Rhino Add-on
folder with:  python3 -m unittest discover tests
"""

import os
import socket
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from projectar import protocol, snapshot, textures  # noqa: E402
from projectar.server import Server  # noqa: E402
from projectar.streaming import Options, Scene, SceneInstance, SceneObject, Streamer  # noqa: E402

from fake_app import FakeApp  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 4, 4) + b"\x08\x06\x00\x00\x00" + b"rest"


def triangle(materials, offset=0.0, triangles=1):
    """A strip of triangles as protocol blobs, like rhino_host.mesh_geometry makes."""
    vertices, indices = [], []
    for t in range(triangles):
        base = len(vertices) // 3
        vertices += [offset + t, 0, 0, offset + t + 1, 0, 0, offset + t, 1, 0]
        indices += [base, base + 1, base + 2]
    count = len(vertices) // 3
    return protocol.Geometry(
        materials=materials, triangles=triangles, vertex_count=count, index_count=len(indices), has_uvs=False,
        blobs=[struct.pack(f"<{len(vertices)}f", *vertices), struct.pack(f"<{len(vertices)}f", *([0, 0, 1] * count)),
               struct.pack(f"<{len(indices)}I", *indices), bytes(4 * triangles)],
    )


def material(name="Oak", color=(0.6, 0.4, 0.2, 1.0), textures_=None):
    info = {"name": name, "color": list(color), "metallic": 0.0, "roughness": 0.5}
    if textures_:
        info["textures"] = textures_
    return info


def matrix(x=0.0):
    return protocol.flatten(protocol.mul(protocol.AXIS, protocol.translation(x, 0, 0), protocol.AXIS_INV))


class FakeHost:
    def __init__(self):
        self.textures = textures.TextureStore(1024, lambda source, role, size: ("png", PNG))
        self.name = "Model"
        self.unit_scale = 0.001
        self.objects = {}
        self.instances = {}
        self.variants, self.notes, self.lights, self.cameras, self.palette = [], [], [], [], []
        self.sync = False
        self.editable = True
        self.applied = []
        self.edits = []
        self.builds = []
        self.snapshot_data = os.urandom(2 * snapshot.CHUNK + 1234)
        self.snapshot_error = None

    def add(self, oid, version=1, x=0.0, materials=None, triangles=1):
        self.objects[oid] = {"version": version, "matrix": matrix(x), "materials": materials or [material()], "triangles": triangles}

    def add_instance(self, iid, proto_key, x=0.0, materials=None, source="Leg"):
        self.instances[iid] = {"key": proto_key, "matrix": matrix(x), "materials": materials or [material("Steel")], "source": source}

    def take_sync_request(self):
        requested, self.sync = self.sync, False
        return requested

    def scene(self, opts):
        objects = {
            oid: SceneObject(
                version=o["version"], matrix=o["matrix"], material_key=protocol.key(o["materials"]),
                materials=(lambda o=o: o["materials"]),
                build=(lambda budget, oid=oid, o=o: self._build(oid, o, budget)),
            )
            for oid, o in self.objects.items()
        }
        instances = {
            iid: SceneInstance(
                proto_key=i["key"], matrix=i["matrix"], source=i["source"], material_key=protocol.key(i["materials"]),
                materials=(lambda i=i: i["materials"]),
                build=(lambda budget, iid=iid, i=i: self._build(iid, i, budget)),
            )
            for iid, i in self.instances.items()
        }
        return Scene(name=self.name, unit_scale=self.unit_scale, objects=objects, instances=instances,
                     variants=self.variants, notes=self.notes, lights=self.lights, cameras=self.cameras,
                     palette=self.palette)

    def _build(self, name, item, budget):
        self.builds.append(name)
        triangles = item.get("triangles", 1)
        if triangles > budget:
            return protocol.empty_geometry(item["materials"], triangles, skipped=True)
        return triangle(item["materials"], triangles=triangles)

    def can_edit(self):
        return self.editable

    def begin_edits(self):
        self.edits.append("begin")

    def end_edits(self, changed):
        self.edits.append(("end", changed))

    def apply_message(self, message):
        self.applied.append(message)
        return message.get("type") != "setVariant" or message.get("option") != "same"

    def on_app_change(self, message):
        pass

    def export_snapshot(self, scene, opts):
        if self.snapshot_error:
            raise snapshot.SnapshotError(self.snapshot_error)
        return self.snapshot_data


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StreamerTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = Server(self.port, host="127.0.0.1")
        self.server.start()
        self.host = FakeHost()
        self.opts = Options(require_approval=False)
        self.streamer = Streamer(self.server, self.host, lambda: self.opts)
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.server.stop()

    def connect(self, **kwargs):
        app = FakeApp(self.port, **kwargs)
        self.apps.append(app)
        return app

    def tick(self):
        self.streamer.tick()

    def synced(self, app):
        self.assertTrue(app.wait(lambda: app.hello is not None and "cameras" in app.extras, tick=self.tick), "no full sync")

    def settle(self, app, seconds=0.25):
        """Ticks for a while (longer than the 0.1 s mesh rate limit)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.tick()
            time.sleep(0.02)

    # ------------------------------------------------------------ tests

    def test_full_sync(self):
        self.host.add("Chair", x=1.0)
        self.host.variants = [{"name": "Sofa", "active": "A", "options": [{"name": "A", "objects": ["Chair"]}]}]
        app = self.connect()
        self.synced(app)
        self.assertEqual(app.hello["scene"], "Model")
        self.assertEqual(app.hello["unitScale"], 0.001)
        self.assertEqual(app.hello["host"], "Rhino")
        self.assertIn("instancing", app.hello["features"])
        self.assertIn("Chair", app.meshes)
        self.assertEqual(app.matrices["Chair"][12], 1.0)  # translation x, column-major
        self.assertEqual(app.extras["variants"][0]["name"], "Sofa")

    def test_move_sends_only_a_transform(self):
        self.host.add("Chair")
        app = self.connect()
        self.synced(app)
        meshes = app.counts["mesh"]
        self.host.objects["Chair"]["matrix"] = matrix(5.0)
        self.assertTrue(app.wait(lambda: app.matrices["Chair"][12] == 5.0, tick=self.tick))
        self.settle(app)
        self.assertEqual(app.counts["mesh"], meshes)

    def test_geometry_change_resends_mesh(self):
        self.host.add("Chair")
        app = self.connect()
        self.synced(app)
        meshes = app.counts["mesh"]
        self.host.objects["Chair"]["version"] = 2
        self.assertTrue(app.wait(lambda: app.counts["mesh"] > meshes, tick=self.tick))

    def test_material_only_change(self):
        self.host.add("Chair")
        updates = self.connect()
        legacy = self.connect(features=("textures",), device_id="OLD")
        self.synced(updates)
        self.synced(legacy)
        before = (updates.counts["mesh"], legacy.counts["mesh"])
        self.host.objects["Chair"]["materials"] = [material("Walnut", (0.3, 0.2, 0.1, 1.0))]
        self.assertTrue(updates.wait(lambda: updates.materials["Chair"][0]["name"] == "Walnut", tick=self.tick))
        self.assertTrue(legacy.wait(lambda: legacy.materials["Chair"][0]["name"] == "Walnut", tick=self.tick))
        self.assertEqual(updates.counts["mesh"], before[0], "apps with materialUpdates get objectMaterials")
        self.assertGreater(legacy.counts["mesh"], before[1], "older apps get the mesh again")

    def test_add_and_remove(self):
        app = self.connect()
        self.synced(app)
        self.host.add("Lamp")
        self.assertTrue(app.wait(lambda: "Lamp" in app.meshes and "Lamp" in app.matrices, tick=self.tick))
        del self.host.objects["Lamp"]
        self.assertTrue(app.wait(lambda: "Lamp" in app.removed, tick=self.tick))
        # Back again (an undo): devices must get its mesh again.
        self.host.add("Lamp")
        self.assertTrue(app.wait(lambda: "Lamp" in app.meshes, tick=self.tick))

    def test_instances_for_instancing_and_older_apps(self):
        for n in range(3):
            self.host.add_instance(f"Table/Leg#{n}", ("leg", 1), x=float(n))
        modern = self.connect()
        older = self.connect(features=("materialUpdates",), device_id="OLD")
        self.synced(modern)
        self.synced(older)
        self.assertEqual(len(modern.prototypes), 1)
        proto = next(iter(modern.prototypes))
        self.assertTrue(proto.startswith("~mesh/"))
        self.assertEqual(set(modern.instances.values()), {proto})
        self.assertEqual(modern.sources["Table/Leg#0"], "Leg")
        self.assertEqual(len(older.meshes), 3, "older apps get one mesh per instance")
        self.assertEqual(len(modern.matrices), 3)
        # The block was edited: a new prototype, the old one removed.
        for item in self.host.instances.values():
            item["key"] = ("leg", 2)
        self.assertTrue(modern.wait(lambda: proto in modern.removed and len(modern.prototypes) == 1
                                    and proto not in modern.prototypes, tick=self.tick))
        self.assertTrue(set(modern.instances.values()) == set(modern.prototypes))

    def test_prototype_material_update(self):
        self.host.add_instance("Table/Leg#0", ("leg", 1))
        app = self.connect()
        self.synced(app)
        proto = next(iter(app.prototypes))
        meshes = app.counts["mesh"]
        self.host.instances["Table/Leg#0"]["materials"] = [material("Brass")]
        self.assertTrue(app.wait(lambda: app.materials[proto][0]["name"] == "Brass", tick=self.tick))
        self.assertEqual(app.counts["mesh"], meshes)

    def test_pause_and_resume(self):
        self.host.add("Chair")
        app = self.connect()
        self.synced(app)
        self.opts.paused = True
        self.host.objects["Chair"]["matrix"] = matrix(3.0)
        self.settle(app)
        self.assertNotEqual(app.matrices["Chair"][12], 3.0)
        late = self.connect(device_id="LATE")
        self.synced(late)
        self.assertEqual(late.matrices["Chair"][12], 3.0, "a device joining while paused gets the current scene")
        self.assertNotEqual(app.matrices["Chair"][12], 3.0)
        self.opts.paused = False
        self.assertTrue(app.wait(lambda: app.matrices["Chair"][12] == 3.0, tick=self.tick))

    def test_extras_change(self):
        app = self.connect()
        self.synced(app)
        self.host.notes = [{"id": "n1", "text": "Too tall", "author": "Ana", "created": "2026-11-11T10:00:00Z", "position": [0, 1, 0]}]
        self.host.lights = [{"id": "Sun", "kind": "sun", "color": [1, 1, 1], "energy": 3.0, "m": matrix()}]
        self.assertTrue(app.wait(lambda: app.extras["notes"] and app.extras["lights"], tick=self.tick))

    def test_textures(self):
        tid = self.host.textures.register(("file", "/tmp/oak.png", 1, 2), "baseColor")
        self.host.add("Chair", materials=[material(textures_={"baseColor": tid})])
        app = self.connect()
        plain = self.connect(features=("materialUpdates",), device_id="PLAIN")
        self.synced(app)
        self.synced(plain)
        self.assertTrue(app.wait(lambda: tid in app.textures, tick=self.tick))
        self.assertEqual(app.textures[tid], PNG)
        self.settle(plain)
        self.assertEqual(plain.textures, {}, "only apps that list textures get them")

    def test_triangle_budget(self):
        self.host.add("Huge", triangles=50)
        self.opts.max_triangles = 10
        app = self.connect()
        self.synced(app)
        self.assertEqual(app.meshes["Huge"]["indexCount"], 0)
        self.assertIn("Huge", self.streamer.skipped)
        self.opts.max_triangles = 100
        self.assertTrue(app.wait(lambda: app.meshes["Huge"]["indexCount"] == 150, tick=self.tick))

    def test_snapshot(self):
        self.host.add("Chair")
        app = self.connect()
        self.synced(app)
        app.send({"type": "requestSnapshot", "id": "SNAP-1"})
        self.assertTrue(app.wait(lambda: app.snapshot_bytes("SNAP-1") is not None, tick=self.tick, timeout=5))
        self.assertEqual(app.snapshot_bytes("SNAP-1"), self.host.snapshot_data)
        self.host.snapshot_error = "nothingToExport"
        app.send({"type": "requestSnapshot", "id": "SNAP-2"})
        self.assertTrue(app.wait(lambda: app.failed and app.failed[-1]["id"] == "SNAP-2", tick=self.tick))
        self.assertEqual(app.failed[-1]["reason"], "nothingToExport")

    def test_app_messages_wait_for_commands(self):
        app = self.connect()
        self.synced(app)
        self.host.editable = False
        app.send({"type": "setVariant", "set": "Sofa", "option": "B"})
        self.settle(app)
        self.assertEqual(self.host.applied, [], "nothing changes while a command runs")
        self.host.editable = True
        self.assertTrue(app.wait(lambda: len(self.host.applied) == 1, tick=self.tick))
        self.assertIn(("end", True), self.host.edits, "one undo step per tick of changes")

    def test_document_change_resyncs(self):
        self.host.add("Chair")
        app = self.connect()
        self.synced(app)
        hellos = app.counts["hello"]
        self.host.name = "Other Model"
        self.assertTrue(app.wait(lambda: app.counts["hello"] > hellos, tick=self.tick))
        self.assertEqual(app.hello["scene"], "Other Model")
        hellos = app.counts["hello"]
        self.host.sync = True
        self.assertTrue(app.wait(lambda: app.counts["hello"] > hellos, tick=self.tick))


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.server = Server(self.port, host="127.0.0.1")
        self.server.start()
        self.host = FakeHost()
        self.host.add("Chair")
        self.opts = Options(require_approval=True)
        self.streamer = Streamer(self.server, self.host, lambda: self.opts)
        self.asked = []
        self.streamer.on_request = self.asked.append
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.server.stop()

    def tick(self):
        self.streamer.tick()

    def test_ask_then_allow(self):
        app = FakeApp(self.port)
        self.apps.append(app)
        self.assertTrue(app.wait(lambda: "waiting" in app.approvals, tick=self.tick))
        self.assertIsNone(app.hello, "a waiting device gets nothing of the document")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0].display_name, "Test iPhone")
        device = self.streamer.answer(self.asked[0].label, True)
        self.assertEqual(device, "id:TEST-DEVICE")
        self.assertTrue(app.wait(lambda: app.hello is not None and "Chair" in app.meshes, tick=self.tick))
        self.assertIn("allowed", app.approvals)

    def test_trusted_device_connects_straight_away(self):
        self.opts.trusted = frozenset({"id:TEST-DEVICE"})
        app = FakeApp(self.port)
        self.apps.append(app)
        self.assertTrue(app.wait(lambda: app.hello is not None, tick=self.tick))
        self.assertEqual(self.asked, [])

    def test_deny(self):
        app = FakeApp(self.port)
        self.apps.append(app)
        self.assertTrue(app.wait(lambda: "waiting" in app.approvals, tick=self.tick))
        self.streamer.answer(self.asked[0].label, False)
        self.assertTrue(app.wait(lambda: "denied" in app.approvals and app.closed, tick=self.tick))
        # It can't ask again straight away.
        again = FakeApp(self.port)
        self.apps.append(again)
        self.assertTrue(again.wait(lambda: "denied" in again.approvals, tick=self.tick))

    def test_messages_from_waiting_devices_are_ignored(self):
        app = FakeApp(self.port)
        self.apps.append(app)
        self.assertTrue(app.wait(lambda: "waiting" in app.approvals, tick=self.tick))
        app.send({"type": "setVariant", "set": "Sofa", "option": "B"})
        app.wait(lambda: False, timeout=0.3, tick=self.tick)
        self.assertEqual(self.host.applied, [])


if __name__ == "__main__":
    unittest.main()
