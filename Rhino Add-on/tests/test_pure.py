# SPDX-License-Identifier: GPL-3.0-or-later
"""Modules without Rhino: study orders and files, decisions, ids, textures,
settings and the USDZ writer. Where the Blender add-on has the same logic,
the results are compared with it, so a study can mix both programs."""

import ast
import io
import json
import os
import struct
import sys
import tempfile
import types
import unittest
import zipfile
import zlib

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
BLENDER = os.path.join(HERE, "..", "..", "Blender Add-on", "viewar")

from projectar import decisions, ids, protocol, settings, snapshot, study, textures  # noqa: E402


def blender_module(name):
    """A Blender add-on module loaded with a stand-in for bpy, for comparing."""
    sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(version_string="5.2")))
    with open(os.path.join(BLENDER, f"{name}.py"), encoding="utf-8") as f:
        source = f.read()
    module = types.ModuleType(f"blender_{name}")
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def png(width, height, rgba=(200, 120, 40, 255)):
    rows = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return b"".join([b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
                     chunk(b"IDAT", zlib.compress(rows)), chunk(b"IEND", b"")])


class MemoryStore:
    def __init__(self):
        self.text, self.photos = None, {}

    def load(self):
        return self.text

    def save(self, value):
        self.text = value

    def photo(self, decision_id):
        return self.photos.get(decision_id)

    def set_photo(self, decision_id, data):
        self.photos[decision_id] = data

    def remove_photo(self, decision_id):
        self.photos.pop(decision_id, None)


class StudyTest(unittest.TestCase):
    def test_orders_match_blender(self):
        blender = blender_module("study")
        for size in range(1, 9):
            for row in range(2 * size + 2):
                self.assertEqual(study.balanced_row(size, row), blender.balanced_row(size, row))
        for participant in ("P01", "P07", "p12", "Anna", "", "P 3 "):
            self.assertEqual(study.suggested_row(participant, 4, 6), blender.suggested_row(participant, 4, 6))
        self.assertEqual(study.EVENT_COLUMNS, blender.EVENT_COLUMNS)
        self.assertEqual(study.TASK_COLUMNS, blender.TASK_COLUMNS)

    def test_session_files(self):
        folder = tempfile.mkdtemp()
        session = study.start(folder, "P02", "Screen", "", ["Oak", "Walnut", "Glass"], True, 1,
                              host={"device": "Rhino", "rhino_version": "8", "file": "/x/model.3dm"})
        self.assertEqual(session.header["device"], "Rhino")
        self.assertEqual(session.header["task_order"], study.task_order(["Oak", "Walnut", "Glass"], True, 1))
        study.start_task()
        study.note_edit("object.transform", "Chair")
        study.flush_bursts(force=True)
        study.end_task()
        path = study.end()
        with open(os.path.join(path, "events.csv"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("object.transform", text)
        self.assertIn("object=Chair", text)
        self.assertTrue(os.path.isfile(os.path.join(folder, study.ALL_TASK_TIMES)))


class DecisionTest(unittest.TestCase):
    def test_add_record_export(self):
        store = MemoryStore()
        jpeg = b"\xff\xd8\xff\xe0" + b"photo"
        import base64

        decision = decisions.add_decision(store, {
            "id": "ABCDEF12-3456", "question": "Which sofa?", "chosen": "B", "options": ["A", "B"],
            "kind": "variant", "set": "Sofa", "criteria": ["Cost"], "reason": "Cheaper",
            "photo": base64.b64encode(jpeg).decode(), "confidence": 4,
        })
        self.assertIsNotNone(decision)
        self.assertEqual(decision["photo"], "decision-abcdef12.jpg")
        self.assertIsNone(decisions.add_decision(store, {"id": "ABCDEF12-3456", "question": "q", "chosen": "c", "options": []}),
                          "the same decision isn't kept twice")
        mine = decisions.record_decision(store, "Finish?", ["Oak", "Glass"], "Oak", "Warmer", author="Dana")
        self.assertEqual(mine["source"], "rhino")
        items = decisions.decisions(store)
        self.assertEqual([d["chosen"] for d in items], ["B", "Oak"])
        csv_path = os.path.join(tempfile.mkdtemp(), "out.csv")
        count, photos = decisions.export_decisions(store, "Model", csv_path)
        self.assertEqual((count, photos), (2, 1))
        with open(csv_path, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        with open(os.path.join(BLENDER, "review.py"), encoding="utf-8") as f:
            source = f.read()
        blender_columns = ast.literal_eval(source.split("CSV_COLUMNS = ", 1)[1].split(")", 1)[0] + ")")
        self.assertEqual(tuple(header), blender_columns)
        self.assertIn("Made in Rhino", decisions.decisions_markdown("Model", items))
        self.assertTrue(decisions.remove_decision(store, "ABCDEF12-3456"))
        self.assertEqual(store.photos, {})


class IdTest(unittest.TestCase):
    def test_names(self):
        registry = ids.IdRegistry()
        self.assertEqual(registry.label("a", "Chair"), "Chair")
        self.assertEqual(registry.label("b", "Chair"), "Chair 2", "a copy keeps its name, numbered")
        self.assertEqual(registry.label("a", "Chair"), "Chair", "ids stay put")
        self.assertEqual(registry.label("c", "Legs/Front#1"), "Legs-Front-1", "no / or # in ids")
        self.assertEqual(registry.label("a", "Stool"), "Stool")
        self.assertEqual(registry.label("d", "Chair"), "Chair", "a freed name is reused")
        self.assertEqual(registry.key_for("Chair 2"), "b")


class TextureTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(textures.image_size(png(37, 12)), (37, 12))
        jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
                + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 480, 640) + b"\x03" + b"\x00" * 9)
        self.assertEqual(textures.image_format(jpeg), "jpeg")
        self.assertEqual(textures.image_size(jpeg), (640, 480))
        self.assertIsNone(textures.image_size(b"nonsense"))

    def test_store_epochs(self):
        loads = []
        store = textures.TextureStore(1024, lambda source, role, size: loads.append(source) or ("png", b"x"))
        tid = store.register(("file", "a.png", 1, 2), "baseColor")
        self.assertIsNotNone(store.frame(tid))
        store.begin_epoch()
        self.assertTrue(store.known(tid), "kept while registered in this epoch")
        store.begin_epoch()
        self.assertFalse(store.known(tid), "forgotten once no material uses it")


class SettingsTest(unittest.TestCase):
    def test_clean(self):
        values = settings.clean({"port": 80, "max_texture_size": 300, "auto_start": 1, "trusted_devices": {"id:x": 5}})
        self.assertEqual(values["port"], 1024)
        self.assertEqual(values["max_texture_size"], 1024)
        self.assertIs(values["auto_start"], True)
        self.assertEqual(values["trusted_devices"], {"id:x": "5"})


class SnapshotTest(unittest.TestCase):
    def test_usdz(self):
        data = make_usdz()
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        self.assertEqual(names[0], "scene.usda", "the layer comes first")
        for info in archive.infolist():
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            name_length, extra_length = struct.unpack("<HH", data[info.header_offset + 26:info.header_offset + 30])
            start = info.header_offset + 30 + name_length + extra_length
            self.assertEqual(start % 64, 0, f"{info.filename} isn't aligned")
        stage = archive.read("scene.usda").decode()
        self.assertIn('upAxis = "Y"', stage)
        self.assertIn("metersPerUnit = 0.001", stage)
        self.assertIn("UsdUVTexture", stage)

    def test_nothing(self):
        with self.assertRaises(snapshot.SnapshotError) as caught:
            snapshot.build_usdz([("Empty", protocol.empty_geometry([]), protocol.flatten(protocol.IDENTITY))],
                                lambda tid: None, 1.0)
        self.assertEqual(caught.exception.reason, "nothingToExport")


def cube(size, materials):
    """A cube in RealityKit axes with per-face corners, UVs and two materials."""
    s = size / 2
    faces = [((0, 0, 1), [(-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]),
             ((0, 0, -1), [(s, -s, -s), (-s, -s, -s), (-s, s, -s), (s, s, -s)]),
             ((1, 0, 0), [(s, -s, s), (s, -s, -s), (s, s, -s), (s, s, s)]),
             ((-1, 0, 0), [(-s, -s, -s), (-s, -s, s), (-s, s, s), (-s, s, -s)]),
             ((0, 1, 0), [(-s, s, s), (s, s, s), (s, s, -s), (-s, s, -s)]),
             ((0, -1, 0), [(-s, -s, -s), (s, -s, -s), (s, -s, s), (-s, -s, s)])]
    positions, normals, uvs, indices, face_materials = [], [], [], [], []
    for f, (normal, corners) in enumerate(faces):
        base = len(positions) // 3
        for corner, uv in zip(corners, [(0, 0), (1, 0), (1, 1), (0, 1)]):
            positions += corner
            normals += normal
            uvs += uv
        indices += [base, base + 1, base + 2, base, base + 2, base + 3]
        face_materials += [0 if f < 4 else 1] * 2
    count = len(positions) // 3
    return protocol.Geometry(
        materials=materials, triangles=12, vertex_count=count, index_count=36, has_uvs=True,
        blobs=[struct.pack(f"<{len(positions)}f", *positions), struct.pack(f"<{len(normals)}f", *normals),
               struct.pack(f"<{len(uvs)}f", *uvs), struct.pack("<36I", *indices), struct.pack("<12I", *face_materials)],
    )


def make_usdz():
    texture = png(64, 64, (180, 90, 40, 255))
    store = textures.TextureStore(1024, lambda source, role, size: ("png", texture))
    tid = store.register(("file", "wood.png", 1, 1), "baseColor")
    wood = {"name": "Wood", "color": [1, 1, 1, 1], "metallic": 0.0, "roughness": 0.6, "textures": {"baseColor": tid}}
    blue = {"name": "Blue Glass", "color": [0.2, 0.4, 0.9, 0.5], "metallic": 0.0, "roughness": 0.1}
    # A 400 mm cube standing on the floor, and a smaller one beside it.
    first = protocol.flatten(protocol.translation(0, 200, 0))
    second = protocol.flatten(protocol.translation(450, 100, 0))
    return snapshot.build_usdz(
        [("Box 1", cube(400, [wood, blue]), first), ("Box 2", cube(200, [blue]), second)], store.data, 0.001)


if __name__ == "__main__":
    unittest.main()
