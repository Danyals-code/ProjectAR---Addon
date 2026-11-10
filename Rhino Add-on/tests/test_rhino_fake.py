# SPDX-License-Identifier: GPL-3.0-or-later
"""The Rhino side against a fake RhinoCommon (fake_rhino): reading a
document, moves and edits, blocks, variants, notes, room scans, decisions,
the camera, snapshots, the whole stream to a fake app, and the window.

This runs every line of the add-on's own Rhino logic. Whether RhinoCommon
really behaves like the fake is for a test in Rhino itself."""

import base64
import io
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zipfile
import zlib

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import fake_rhino as fake  # noqa: E402

fake.install()

from projectar import app as app_module  # noqa: E402
from projectar import discovery, protocol, rhino_host, settings, streaming, study  # noqa: E402
from projectar.server import Server  # noqa: E402

from fake_app import FakeApp  # noqa: E402


def png_file(folder):
    rows = b"".join(b"\x00" + bytes((120, 120, 130, 255)) * 8 for _ in range(8))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    data = b"".join([b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 6, 0, 0, 0)),
                     chunk(b"IDAT", zlib.compress(rows)), chunk(b"IEND", b"")])
    path = os.path.join(folder, "brushed.png")
    with open(path, "wb") as f:
        f.write(data)
    return path


def box(x0, y0, z0, x1, y1, z1):
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    return fake.Brep(fake.box_mesh(corners))


def attributes(layer=0, name="", material=None, source=None):
    a = fake.ObjectAttributes()
    a.LayerIndex = layer
    a.Name = name
    if material is not None:
        a.MaterialSource = fake.ObjectMaterialSource.MaterialFromObject
        a.MaterialIndex = material
    if source is not None:
        a.MaterialSource = getattr(fake.ObjectMaterialSource, source)
    return a


def build_doc(folder):
    doc = fake.RhinoDoc()
    fake.RhinoDoc.ActiveDoc = doc
    furniture = doc.layer("Furniture")
    sofa = doc.layer("Sofa", parent=furniture)
    option_a = doc.layer("A", parent=sofa)
    option_b = doc.layer("B", parent=sofa, visible=False)
    hidden = doc.layer("Hidden", visible=False)

    oak = fake.Material()
    oak.Name = "Oak"
    oak.DiffuseColor = fake.Color(255, 160, 110, 60)
    oak.Shine = 64
    steel = fake.Material()
    steel.Name = "Steel"
    steel.ToPhysicallyBased()
    steel.PhysicallyBased.BaseColor = fake.Color4f(0.5, 0.5, 0.55, 1.0)
    steel.PhysicallyBased.Metallic = 1.0
    steel.PhysicallyBased.Roughness = 0.3
    steel.textures["PBR_BaseColor"] = fake.Texture(png_file(folder))
    oak_index = doc.Materials.Add(oak)
    steel_index = doc.Materials.Add(steel)

    objects = doc.Objects
    table = objects.add_geometry(box(0, 0, 0, 1200, 800, 750), attributes(furniture, "Table", material=oak_index))
    triangle = fake.Mesh()
    triangle.vertices = [(0, 0, 0), (100, 0, 0), (0, 100, 0)]
    triangle.normals = [(0, 0, 1)] * 3
    triangle.faces = [(0, 1, 2)]
    objects.add_geometry(triangle, attributes(0))
    objects.add_geometry(box(0, 0, 0, 2000, 900, 800), attributes(option_a, "Sofa A"))
    objects.add_geometry(box(0, 0, 0, 1800, 900, 800), attributes(option_b, "Sofa B"))
    objects.add_geometry(box(0, 0, 0, 10, 10, 10), attributes(hidden, "Ghost"))
    kept_out = attributes(0, "Reference Plane")
    kept_out.SetUserString(rhino_host.HIDE_KEY, "1")
    objects.add_geometry(box(0, 0, 0, 10, 10, 10), kept_out)

    leg_mesh = fake.Mesh()
    leg_mesh.vertices = [(0, 0, 0), (30, 0, 0), (0, 30, 400)]
    leg_mesh.normals = [(1, 0, 0)] * 3
    leg_mesh.faces = [(0, 1, 2)]
    leg_def = doc.InstanceDefinitions.add("Leg", [fake.RhinoObject(doc, leg_mesh, attributes(0), fake.ObjectType.Mesh)])
    seat = fake.RhinoObject(doc, box(0, 0, 400, 450, 450, 450), attributes(0, "Seat", source="MaterialFromParent"),
                            fake.ObjectType.Brep)
    nested = fake.InstanceObject(doc, leg_def, fake.Transform.Translation(10, 10, 0), attributes(0))
    chair_def = doc.InstanceDefinitions.add("Chair", [seat, nested])
    for x in (3000, 4000):
        objects.add(fake.InstanceObject(doc, chair_def, fake.Transform.Translation(x, 0, 0),
                                        attributes(furniture, material=steel_index)))

    note = attributes(0, "Note: Too tall")
    note.SetUserString(rhino_host.NOTE_ID, "note-1")
    note.SetUserString(rhino_host.NOTE_AUTHOR, "Ana")
    note.SetUserString(rhino_host.NOTE_CREATED, "2026-11-11T10:00:00Z")
    objects.add_geometry(fake.TextDot("Too tall", fake.Point3d(100, 200, 300)), note)

    doc.Lights.lights.append(fake.LightObject(doc, fake.Light("point", (0, 0, 2500)), attributes(0, "Lamp")))
    doc.NamedViews.add("Front", fake.RhinoViewport("Front"))
    return doc, {"furniture": furniture, "sofa": sofa, "a": option_a, "b": option_b, "steel": steel_index,
                 "table": table, "chair_def": chair_def}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def floats(blob):
    return struct.unpack(f"<{len(blob) // 4}f", blob)


class HostTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.doc, self.ids = build_doc(self.folder)
        self.host = rhino_host.RhinoHost()
        self.opts = streaming.Options(require_approval=False)

    def tearDown(self):
        self.host.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def scene(self):
        return self.host.scene(self.opts)

    def test_reading_a_document(self):
        scene = self.scene()
        self.assertEqual(scene.name, "Model")
        self.assertEqual(scene.unit_scale, 1.0, "devices get meters, whatever the document's units")
        self.assertEqual(set(scene.objects), {"Table", "Mesh", "Sofa A"},
                         "hidden layers, Show in AR off, notes and blocks aren't objects")
        table = scene.objects["Table"].materials()[0]
        self.assertEqual(table["name"], "Oak")
        self.assertAlmostEqual(table["color"][0], 160 / 255)
        self.assertAlmostEqual(table["roughness"], 1 - 64 / 255)
        self.assertEqual(scene.objects["Mesh"].materials()[0]["name"], "Display Color")
        self.assertEqual(sorted(scene.instances), ["Chair 2/Leg Mesh#1.0", "Chair 2/Seat#0", "Chair/Leg Mesh#1.0", "Chair/Seat#0"])
        seat = scene.instances["Chair/Seat#0"]
        self.assertEqual(seat.source, "Seat")
        self.assertEqual(seat.materials()[0]["name"], "Steel", "by parent: the instance's material")
        self.assertIn("baseColor", seat.materials()[0]["textures"])
        self.assertEqual(seat.proto_key, scene.instances["Chair 2/Seat#0"].proto_key, "one prototype for both chairs")
        self.assertEqual(scene.instances["Chair 2/Seat#0"].matrix[12], 4.0, "4000 mm")
        leg = scene.instances["Chair/Leg Mesh#1.0"]
        self.assertEqual(leg.matrix[12], 3.01, "nested block transforms combine")
        self.assertEqual(leg.matrix[0], 1.0, "matrices carry no scale: meshes are already in meters")
        geometry = leg.build(1000)
        for a, b in zip(floats(geometry.blobs[0])[6:9], (0.0, 0.4, -0.03)):
            self.assertAlmostEqual(a, b, places=6, msg="Rhino (x, y, z) mm is RealityKit (x, z, -y) m")
        self.assertEqual(scene.notes[0]["position"], [0.1, 0.3, -0.2])
        lamp = scene.lights[0]
        self.assertEqual(lamp["kind"], "point")
        self.assertEqual(lamp["energy"], 1000.0)
        self.assertEqual(lamp["m"][13], 2.5, "the lamp is 2.5 m up, within the app's 20 m light range")
        self.assertEqual(lamp["m"][0], 1.0)
        camera = scene.cameras[0]
        self.assertEqual(camera["clip"], [0.001, 10.0], "clipping in meters too")
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in camera["m"][0:3])), 1.0, places=5)
        self.assertEqual([c["id"] for c in scene.cameras], ["Perspective", "Front"])
        self.assertTrue(scene.cameras[0]["active"])
        self.assertIn("Oak", [m["name"] for m in scene.palette])

    def test_light_power(self):
        light = self.doc.Lights.lights[0].LightGeometry
        light.PowerWatts = 60.0
        self.host._lights_dirty = True
        self.assertEqual(self.scene().lights[0]["energy"], 60.0, "Rhino's own watts when it has them")
        light.PowerWatts = 0.0
        light.Intensity = 0.5
        self.host._lights_dirty = True
        self.assertEqual(self.scene().lights[0]["energy"], 500.0, "otherwise intensity scales Blender's default")

    def test_units_change(self):
        before = self.scene().objects["Table"]
        self.assertFalse(self.host.take_sync_request())
        self.doc.ModelUnitSystem = fake.UnitSystem.Meters
        fake.RhinoDoc.DocumentPropertiesChanged.fire(self.doc, None)
        after = self.scene().objects["Table"]
        self.assertTrue(self.host.take_sync_request(), "other units: devices get everything again")
        self.assertNotEqual(after.version, before.version)
        corner = floats(after.build(1000).blobs[0])[3:6]
        self.assertEqual(corner, (1200.0, 0.0, 0.0), "the same numbers, now read as meters")

    def test_variant_sets(self):
        self.doc.Layers.CurrentLayerIndex = self.ids["sofa"]
        self.assertIsNone(self.host.mark_variant_set())
        scene = self.scene()
        self.assertIn("Sofa B", scene.objects, "every option is streamed so the app can switch instantly")
        sets = scene.variants
        self.assertEqual(sets[0]["name"], "Sofa")
        self.assertEqual(sets[0]["active"], "A")
        self.assertEqual([o["objects"] for o in sets[0]["options"]], [["Sofa A"], ["Sofa B"]])
        self.assertTrue(self.host.apply_message({"type": "setVariant", "set": "Sofa", "option": "B"}))
        self.assertEqual(self.scene().variants[0]["active"], "B")
        self.assertFalse(self.host.apply_message({"type": "setVariant", "set": "Sofa", "option": "B"}), "already showing")
        self.assertTrue(self.host.unmark_variant_set("Sofa"))
        self.assertEqual(self.scene().variants, [])

    def test_hidden_options_keep_their_sublayers(self):
        """Rhino turns sublayers of a hidden option off; they still stream,
        as they'll show when the option does."""
        detail = self.doc.layer("Cushions", parent=self.ids["b"])
        self.doc.Objects.add_geometry(box(0, 0, 0, 500, 500, 100), attributes(detail, "Cushion"))
        self.assertFalse(self.doc.Layers.layers[detail].IsVisible)
        self.doc.Layers.CurrentLayerIndex = self.ids["sofa"]
        self.assertIsNone(self.host.mark_variant_set())
        scene = self.scene()
        self.assertIn("Cushion", scene.objects)
        self.assertIn("Cushion", scene.variants[0]["options"][1]["objects"])
        self.assertTrue(self.host.apply_message({"type": "setVariant", "set": "Sofa", "option": "B"}))
        self.assertTrue(self.doc.Layers.layers[detail].IsVisible, "shown with its option")
        self.assertFalse(self.doc.Layers.layers[self.ids["a"]].GetPersistentVisibility())

    def test_trying_on_a_document_material(self):
        self.scene()
        self.assertTrue(self.host.apply_message({"type": "setMaterial", "object": "Table", "slot": 0, "material": "Steel"}))
        self.assertEqual(self.scene().objects["Table"].materials()[0]["name"], "Steel")
        self.assertFalse(self.host.apply_message({"type": "setMaterial", "object": "Table", "slot": 0, "material": "Steel"}),
                         "already assigned")

    def test_import_is_not_another_document(self):
        self.scene()
        fake.RhinoDoc.EndOpenDocument.fire(self.doc, types.SimpleNamespace(Merge=True, Reference=False))
        self.scene()
        self.assertFalse(self.host.take_sync_request(), "an import adds objects; it doesn't reset devices")
        fake.RhinoDoc.EndOpenDocument.fire(self.doc, types.SimpleNamespace(Merge=False, Reference=False))
        self.scene()
        self.assertTrue(self.host.take_sync_request())

    def test_handler_errors_stay_out_of_rhino(self):
        self.host._records = None  # any handler touching it now fails
        fake.RhinoDoc.DeselectAllObjects.fire(self.doc, None)  # must not raise into Rhino
        self.host._records = {}

    def test_move_keeps_the_mesh(self):
        scene = self.scene()
        version = scene.objects["Table"].version
        before = floats(scene.objects["Table"].build(1000).blobs[0])
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        self.doc.Objects.move(table, fake.Transform.Translation(100, 50, 0))
        scene = self.scene()
        self.assertEqual(scene.objects["Table"].version, version, "a move sends a matrix, not a mesh")
        m = scene.objects["Table"].matrix
        self.assertEqual((m[12], m[13], m[14]), (0.1, 0.0, -0.05), "100 mm along x, 50 along y, in meters")
        after = floats(scene.objects["Table"].build(1000).blobs[0])
        for a, b in zip(before, after):
            self.assertAlmostEqual(a, b, places=3)
        # A rotation is a move too.
        turn = fake.Transform(1.0)
        turn.M00, turn.M01, turn.M10, turn.M11 = 0.0, -1.0, 1.0, 0.0
        self.doc.Objects.move(self.doc.Objects.FindId(self.ids["table"].Id), turn)
        self.assertEqual(self.scene().objects["Table"].version, version)
        # Stretching changes the shape: the mesh goes again.
        stretch = fake.Transform(1.0)
        stretch.M00 = 2.0
        self.doc.Objects.move(self.doc.Objects.FindId(self.ids["table"].Id), stretch)
        scene = self.scene()
        self.assertNotEqual(scene.objects["Table"].version, version)
        self.assertEqual(scene.objects["Table"].matrix, protocol.flatten(protocol.IDENTITY))

    def test_edits_materials_and_deletes(self):
        version = self.scene().objects["Table"].version
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        self.doc.Objects.replace(table, box(0, 0, 0, 1000, 800, 750))
        scene = self.scene()
        self.assertNotEqual(scene.objects["Table"].version, version, "an edit sends the mesh again")
        version = scene.objects["Table"].version
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        attrs = table.Attributes.Duplicate()
        attrs.MaterialIndex = self.ids["steel"]
        key = scene.objects["Table"].material_key
        self.doc.Objects.ModifyAttributes(table, attrs, True)
        scene = self.scene()
        self.assertEqual(scene.objects["Table"].version, version)
        self.assertNotEqual(scene.objects["Table"].material_key, key)
        self.doc.Objects.Delete(table.Id, True)
        self.assertNotIn("Table", self.scene().objects)

    def test_selected_and_layer_scopes(self):
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        self.opts.scope = "SELECTED"
        self.assertEqual(set(self.scene().objects), set())
        self.doc.Objects.Select(table.Id, True)
        self.assertEqual(set(self.scene().objects), {"Table"})
        self.opts.scope = "LAYER"
        self.opts.layer = "Furniture"
        self.assertEqual(set(self.scene().objects), {"Table", "Sofa A"})
        self.assertEqual(len(self.scene().instances), 4)

    def test_app_changes(self):
        self.scene()
        # A look made in the app, applied to the table.
        definition = {"key": "custom:1", "name": "Terracotta", "color": [0.8, 0.4, 0.3, 1.0], "metallic": 0.0, "roughness": 0.7}
        self.assertTrue(self.host.apply_message({"type": "setMaterial", "object": "Table", "slot": 0, "definition": definition}))
        info = self.scene().objects["Table"].materials()[0]
        self.assertEqual((info["name"], info["key"]), ("Terracotta", "custom:1"))
        self.assertFalse(self.host.apply_message({"type": "setMaterial", "object": "Table", "slot": 0, "definition": definition}),
                         "the same look again changes nothing")
        definition["color"] = [0.2, 0.4, 0.8, 1.0]
        self.assertTrue(self.host.apply_message({"type": "setMaterial", "object": "Table", "slot": 0, "definition": definition}))
        self.assertAlmostEqual(self.scene().objects["Table"].materials()[0]["color"][2], 0.8)
        self.assertEqual(sum(1 for m in self.doc.Materials.materials if m.GetUserString(rhino_host.MATERIAL_KEY) == "custom:1"), 1)
        # A document material on an object inside a block: the block changes.
        self.assertTrue(self.host.apply_message({"type": "setMaterial", "object": "Seat", "slot": 0, "material": "Oak"}))
        self.assertEqual(self.scene().instances["Chair/Seat#0"].materials()[0]["name"], "Oak")
        self.assertFalse(self.host.apply_message({"type": "setMaterial", "object": "Table", "slot": 1, "material": "Oak"}))
        # Notes.
        self.assertTrue(self.host.apply_message({"type": "addNote", "id": "n2", "text": "Lower this", "author": "Ben",
                                                 "created": "2026-11-11T11:00:00Z", "position": [0.01, 0.02, -0.03], "object": "Table"}))
        notes = {n["id"]: n for n in self.scene().notes}
        self.assertEqual(notes["n2"]["position"], [0.01, 0.02, -0.03])
        dot = next(o for o in self.doc.Objects.objects if o.Attributes.GetUserString(rhino_host.NOTE_ID) == "n2")
        self.assertEqual((round(dot.Geometry.Point.X, 6), round(dot.Geometry.Point.Y, 6), round(dot.Geometry.Point.Z, 6)),
                         (10.0, 30.0, 20.0), "the note lands in millimeters")
        self.assertEqual(notes["n2"]["object"], "Table")
        self.assertFalse(self.host.apply_message({"type": "addNote", "id": "n2", "text": "again", "position": [0, 0, 0]}))
        self.assertTrue(self.host.apply_message({"type": "removeNote", "id": "n2"}))
        self.assertNotIn("n2", {n["id"] for n in self.scene().notes})
        # A room scan: boxes on their own layers, kept out of AR.
        # The app sends meters, as it was told the unit is 1.
        wall = protocol.flatten(protocol.translation(0, 1.35, -2.0))
        floor = protocol.flatten(protocol.IDENTITY)
        scan = {"type": "addRoomScan", "id": "SCAN", "name": "Room Scan", "aligned": True, "items": [
            {"kind": "wall", "transform": wall, "dimensions": [4.0, 2.7, 0.1]},
            {"kind": "floor", "transform": floor, "dimensions": [4.0, 0.01, 3.0],
             "polygon": [[-2.0, 0, -1.5], [2.0, 0, -1.5], [2.0, 0, 1.5], [-2.0, 0, 1.5]]},
            {"kind": "object", "category": "washer_dryer", "transform": floor, "dimensions": [0.6, 0.85, 0.6]},
            {"kind": "nonsense", "transform": floor, "dimensions": [1, 1, 1]},
        ]}
        self.assertTrue(self.host.apply_message(scan))
        scene = self.scene()
        self.assertNotIn("Wall 1", scene.objects, "scan objects stay out of AR")
        names = {o.Attributes.Name for o in self.doc.Objects.objects if not o.IsDeleted}
        self.assertTrue({"Wall 1", "Floor", "Washer Dryer 1"} <= names)
        paths = self.host.layer_paths()
        self.assertTrue({"Room Scan", "Room Scan::Walls", "Room Scan::Floors", "Room Scan::Furniture"} <= set(paths))
        wall_obj = next(o for o in self.doc.Objects.objects if o.Attributes.Name == "Wall 1")
        zs = [v[2] for v in wall_obj.Geometry.mesh.vertices]
        self.assertAlmostEqual(min(zs), 0.0)
        self.assertAlmostEqual(max(zs), 2700.0, msg="2.7 m tall, in millimeters, with RealityKit up as Rhino Z")
        xs = [v[0] for v in wall_obj.Geometry.mesh.vertices]
        self.assertAlmostEqual(max(xs) - min(xs), 4000.0)
        floor_obj = next(o for o in self.doc.Objects.objects if o.Attributes.Name == "Floor")
        self.assertAlmostEqual(max(v[1] for v in floor_obj.Geometry.vertices), 1500.0)
        # Decisions.
        jpeg = b"\xff\xd8\xff\xe0photo"
        self.assertTrue(self.host.apply_message({"type": "addDecision", "id": "D1", "question": "Which sofa?",
                                                 "options": ["A", "B"], "chosen": "B", "kind": "variant", "set": "Sofa",
                                                 "reason": "Fits", "photo": base64.b64encode(jpeg).decode()}))
        self.assertEqual(self.host.decisions()[0]["chosen"], "B")
        self.assertEqual(self.host.decision_store.photo("D1"), jpeg)
        csv_path = os.path.join(self.folder, "decisions.csv")
        self.assertEqual(self.host.export_decisions(csv_path), (1, 1))
        self.assertTrue(self.host.apply_message({"type": "removeDecision", "id": "D1"}))
        # The camera moves to the phone.
        pose = protocol.flatten(protocol.translation(0.5, 1.6, 3.0))  # meters
        self.assertTrue(self.host.apply_message({"type": "setCamera", "m": pose}))
        vp = self.doc.Views.ActiveView.ActiveViewport
        self.assertEqual((vp.CameraLocation.X, vp.CameraLocation.Y, vp.CameraLocation.Z), (500.0, -3000.0, 1600.0))
        direction = vp.CameraDirection
        self.assertGreater(direction.Y, 0.99 * direction.Length, "the phone looked along -Z, Rhino's +Y")
        self.assertFalse(self.host.apply_message({"type": "setCamera", "m": [0] * 16}))
        self.assertFalse(self.host.apply_message({"type": "unknownThing"}))

    def test_origin_and_show_in_ar(self):
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        table.selected = True
        self.assertTrue(self.host.set_origin_from_selection())
        self.opts.origin = "POINT"
        m = self.scene().objects["Table"].matrix
        self.assertEqual((m[12], m[13], m[14]), (-0.6, 0.0, 0.4), "the selection's bottom center is the origin")
        self.assertEqual(self.host.set_ar_hidden(True), 1)
        self.assertNotIn("Table", self.scene().objects)
        self.assertEqual(self.host.set_ar_hidden(False), 1)
        self.assertIn("Table", self.scene().objects)

    def test_snapshot(self):
        scene = self.scene()
        data = self.host.export_snapshot(scene, self.opts)
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(archive.namelist()[0], "scene.usda")
        self.assertTrue(any(n.startswith("textures/") for n in archive.namelist()))
        if os.path.exists("/usr/bin/usdchecker"):
            path = os.path.join(self.folder, "snapshot.usdz")
            with open(path, "wb") as f:
                f.write(data)
            result = subprocess.run(["/usr/bin/usdchecker", "--arkit", path], capture_output=True, text=True)
            self.assertIn("Success", result.stdout + result.stderr, result.stdout + result.stderr)

    def test_document_switch(self):
        self.scene()
        self.assertFalse(self.host.take_sync_request(), "the first document isn't a switch")
        other, _ = build_doc(self.folder)
        other.Name = "Other.3dm"
        scene = self.scene()
        self.assertEqual(scene.name, "Other")
        self.assertTrue(self.host.take_sync_request(), "another document: devices get a full sync")


class TransformTest(unittest.TestCase):
    def test_transform_without_setting_fields(self):
        """If Python can't set a Transform's fields, rotations, scales and moves are still built."""
        original = fake.Transform.__setattr__

        def stubborn(self, name, value):
            if not (name.startswith("M") and hasattr(self, "M33")):
                object.__setattr__(self, name, value)

        m = protocol.mul(protocol.translation(5, -2, 9), protocol.AXIS, ((2.0, 0, 0, 0), (0, 2.0, 0, 0), (0, 0, 2.0, 0), (0, 0, 0, 1)))
        fake.Transform.__setattr__ = stubborn
        try:
            xf = rhino_host.to_transform(m)
        finally:
            fake.Transform.__setattr__ = original
        for row_a, row_b in zip(rhino_host.to_matrix(xf), m):
            for a, b in zip(row_a, row_b):
                self.assertAlmostEqual(a, b)
        self.assertEqual(rhino_host.to_matrix(rhino_host.to_transform(m)), tuple(tuple(r) for r in m))


class StreamTest(unittest.TestCase):
    """The whole path: fake Rhino document -> host -> streamer -> socket -> fake app."""

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.doc, self.ids = build_doc(self.folder)
        self.host = rhino_host.RhinoHost()
        self.port = free_port()
        self.server = Server(self.port, host="127.0.0.1")
        self.server.start()
        self.opts = streaming.Options(require_approval=False)
        self.streamer = streaming.Streamer(self.server, self.host, lambda: self.opts)
        self.app = FakeApp(self.port)

    def tearDown(self):
        self.app.close()
        self.server.stop()
        self.host.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def tick(self):
        self.streamer.tick()

    def test_stream(self):
        app = self.app
        self.assertTrue(app.wait(lambda: app.hello is not None and "cameras" in app.extras, tick=self.tick))
        self.assertEqual(set(app.meshes), {"Table", "Mesh", "Sofa A"})
        self.assertEqual(len(app.prototypes), 2)
        self.assertEqual(len(app.instances), 4)
        self.assertTrue(app.wait(lambda: len(app.textures) == 1, tick=self.tick), "the steel texture")
        meshes = app.counts["mesh"]
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        self.doc.Objects.move(table, fake.Transform.Translation(250, 0, 0))
        self.assertEqual(app.hello["unitScale"], 1.0)
        self.assertTrue(app.wait(lambda: app.matrices["Table"][12] == 0.25, tick=self.tick), "250 mm, in meters")
        time.sleep(0.15)
        self.tick()
        self.assertEqual(app.counts["mesh"], meshes, "moving sent no mesh")
        app.send({"type": "setMaterial", "object": "Table", "slot": 0,
                  "definition": {"key": "builtin:Walnut", "name": "Walnut", "color": [0.3, 0.2, 0.1, 1], "metallic": 0, "roughness": 0.6}})
        self.assertTrue(app.wait(lambda: app.materials["Table"][0].get("key") == "builtin:Walnut", tick=self.tick))
        self.assertEqual(app.counts["mesh"], meshes, "a material change sends objectMaterials")
        app.send({"type": "addNote", "id": "n9", "text": "Hi", "author": "Ana", "created": "2026-11-11T12:00:00Z", "position": [1, 2, 3]})
        self.assertTrue(app.wait(lambda: any(n["id"] == "n9" for n in app.extras["notes"]), tick=self.tick))
        app.send({"type": "requestSnapshot", "id": "S1"})
        self.assertTrue(app.wait(lambda: app.snapshot_bytes("S1") is not None, tick=self.tick, timeout=5))
        self.assertEqual(zipfile.ZipFile(io.BytesIO(app.snapshot_bytes("S1"))).namelist()[0], "scene.usda")


def walk(widget):
    """Every control in an Eto layout."""
    if widget is None:
        return
    yield widget
    for page in getattr(widget, "Pages", []) or []:
        yield from walk(page)
    for item in getattr(widget, "Items", []) if isinstance(widget, fake.StackLayout) else []:
        yield from walk(item.Control)
    yield from walk(getattr(widget, "Content", None))


def buttons(widget):
    return {b.Text: b for b in walk(widget) if isinstance(b, fake.Button)}


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.doc, self.ids = build_doc(self.folder)
        self._patches = []

        def patch(owner, name, value):
            self._patches.append((owner, name, getattr(owner, name)))
            setattr(owner, name, value)

        patch(settings, "folder", lambda: os.path.join(self.folder, "settings"))
        patch(discovery.Advertiser, "start", lambda self, port, addresses: None)
        port = free_port()

        class LocalServer(Server):
            def __init__(self, _port, host="127.0.0.1"):
                super().__init__(port, host)

        patch(app_module.server, "Server", LocalServer)
        patch(app_module, "ANSWER_DELAY", 0.0)
        patch(study, "default_folder", lambda: os.path.join(self.folder, "study"))
        self.port = port
        sys.modules["scriptcontext"].sticky.clear()
        self.controller = app_module.controller()
        self.controller.show()
        self.window = self.controller.window

    def tearDown(self):
        self.controller.stop()
        self.controller.host.close()
        if study.active() is not None:
            study.end()
        for owner, name, value in reversed(self._patches):
            setattr(owner, name, value)
        shutil.rmtree(self.folder, ignore_errors=True)

    def tick(self):
        self.controller._last_ui = 0.0
        self.controller._timer.Elapsed.fire(None, None)

    def click(self, text):
        found = buttons(self.window.form)
        self.assertIn(text, found, f"no {text} button in {sorted(found)}")
        found[text].click()
        self.tick()

    def test_connect_allow_and_tools(self):
        self.click("Start Server")
        self.assertTrue(self.controller.running)
        app = FakeApp(self.port)
        try:
            self.assertTrue(app.wait(lambda: "waiting" in app.approvals, tick=self.tick))
            self.tick()
            self.click("Allow")
            self.assertTrue(app.wait(lambda: app.hello is not None, tick=self.tick))
            self.assertIn("id:TEST-DEVICE", self.controller.settings["trusted_devices"])
            self.assertTrue(os.path.isfile(settings.path()), "allowed devices are saved")
            self.click("Copy Address")
            self.assertEqual(fake.Clipboard.text, f"{self.controller.addresses[0]}:{self.port}")
            for text in ("Resync", "Pause Updates", "Resume Updates", "Send Snapshot", "Use Selection", "Pick Point",
                         "Clear", "Keep Selected Out", "Show Selected"):
                self.click(text)
            self.assertTrue(app.wait(lambda: app.snapshots, tick=self.tick))
            labels = [w.Text for w in walk(self.window.form) if isinstance(w, fake.Label)]
            self.assertTrue(any(t.startswith("Triangles:") for t in labels), "statistics show")
        finally:
            app.close()
        self.click("Stop")
        self.assertFalse(self.controller.running)

    def test_review_tools(self):
        self.doc.Layers.CurrentLayerIndex = self.ids["sofa"]
        self.click("Use Current Layer as Variant Set")
        self.click("B")
        self.assertEqual(self.controller.host.variant_sets()[0]["active"], "B")
        self.window.note_text.Text = "Check the height"
        self.doc.Objects.FindId(self.ids["table"].Id).selected = True
        self.click("Add Note at Selection")
        note = next(n for n in self.controller.host.notes() if n["text"] == "Check the height")
        self.assertEqual(note["object"], "Table")

        def fill(dialog):
            window = self.window
            dialog_buttons = buttons(dialog)
            reasons = [w for w in walk(dialog) if isinstance(w, fake.TextArea)]
            reasons[0].Text = "Shorter, fits the room"
            checks = [w for w in walk(dialog) if isinstance(w, fake.CheckBox)]
            checks[0].Checked = True
            dialog_buttons["Record"].click()
            self.assertFalse(dialog.shown and False)
            window.recorded = True

        fake.Dialog.on_show = fill
        try:
            self.click("Record Decision")
        finally:
            fake.Dialog.on_show = None
        decision = self.controller.host.decisions()[-1]
        self.assertEqual(decision["set"], "Sofa")
        self.assertEqual(decision["question"], "Which option for Sofa?")
        self.assertEqual(decision["chosen"], "B")
        self.assertEqual(decision["criteria"], ["Fits the space"])
        self.assertEqual(decision["source"], "rhino")
        self.assertIsNotNone(self.controller.host.decision_store.photo(decision["id"]), "the view's image is kept")
        self.tick()
        self.click("Open Log")
        fake._FileDialog.path = os.path.join(self.folder, "out.csv")
        self.click("Export CSV")
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "out.csv")))
        self.click("Show Chosen")
        self.click("Photo")
        self.click("Delete")  # the first Delete is a note's
        self.click("Stop Using")
        self.assertEqual(self.controller.host.variant_sets(), [])

    def test_study_session(self):
        self.controller.settings["study_tasks"] = "Oak, Walnut, Glass"
        self.controller.study_form["participant"] = "P02"
        self.tick()
        self.click("Start Session")
        self.assertIsNone(study.active(), "no session without consent")
        self.controller.study_form["consent"] = True
        self.click("Start Session")
        session = study.active()
        self.assertIsNotNone(session)
        first = session.next_task
        self.click(f"Start {first}")
        table = self.doc.Objects.FindId(self.ids["table"].Id)
        self.doc.Objects.move(table, fake.Transform.Translation(10, 0, 0))
        self.tick()
        self.click(f"End {first}")
        self.click("Add Marker")
        self.click("End Session")
        self.assertIsNone(study.active())
        folder = study.sessions_in(os.path.join(self.folder, "study"))[-1]
        with open(os.path.join(folder, "events.csv"), encoding="utf-8") as f:
            events = f.read()
        self.assertIn("object.transform", events)
        self.assertIn("task.end", events)


if __name__ == "__main__":
    unittest.main()
