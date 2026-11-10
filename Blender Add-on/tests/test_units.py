# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything goes to the app in meters, whatever the scene's Unit Scale.

Runs inside Blender, with a fake app on 127.0.0.1:

    blender -b --factory-startup --python-exit-code 1 --python "Blender Add-on/tests/test_units.py"
"""

import math
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
from mathutils import Matrix  # noqa: E402

from stream_scene import Streaming, build_scene, collect, summary  # noqa: E402


def column_major(m):
    return [m[row][col] for col in range(4) for row in range(4)]


class UnitsTest(unittest.TestCase):
    def test_same_room_in_meters_and_millimeters(self):
        """A scene modeled in millimeters reaches the app exactly as the same
        scene modeled in meters: sizes, positions, lights, cameras, notes."""
        meters = collect(1.0)
        millimeters = collect(0.001)
        self.assertEqual(meters["unitScale"], 1.0)
        self.assertEqual(millimeters["unitScale"], 1.0, "the app is always told meters")
        self.assertEqual(millimeters["meshes"], meters["meshes"])
        self.assertEqual(millimeters["prototypes"], meters["prototypes"])
        self.assertEqual(millimeters["matrices"], meters["matrices"])
        self.assertEqual(millimeters["notes"], meters["notes"])
        self.assertEqual(millimeters["cameras"], meters["cameras"])
        for a, b in zip(millimeters["lights"], meters["lights"]):
            self.assertEqual(a.keys(), b.keys())
            for key in a:
                if key == "energy":
                    # Blender keeps Unit Scale as a 32-bit float: 0.001 is 0.0010000000475.
                    self.assertAlmostEqual(a[key], b[key], delta=b[key] * 1e-6)
                else:
                    self.assertEqual(a[key], b[key], key)
        lamp = next(l for l in millimeters["lights"] if l["id"] == "Lamp")
        self.assertEqual(lamp["m"][13], 3.0, "3 m up, inside the app's 20-unit light range")
        self.assertAlmostEqual(lamp["energy"], 1000.0, delta=1e-3, msg="as many watts as the same lamp in meters")
        self.assertEqual(lamp["radius"], 0.25)
        self.assertEqual(millimeters["matrices"]["Cube"][0], 1.0, "no scale in matrices")
        self.assertEqual(millimeters["meshes"]["Cube"]["positions"][:3], [-1.0, -1.0, 1.0])

    def test_app_changes_land_in_blender_units(self):
        scene = build_scene(0.001)
        run = Streaming()
        try:
            self.assertTrue(run.synced())
            app = run.app
            # A note half a meter along X, in the app's meters.
            app.send({"type": "addNote", "id": "n2", "text": "Lower", "author": "Ben",
                      "created": "2026-11-11T11:00:00Z", "position": [0.5, 0.0, 0.0]})
            # A wall 4 m wide and 2.7 m tall, 2 m away, standing on the floor.
            wall = Matrix.Translation((0.0, 1.35, -2.0))
            app.send({"type": "addRoomScan", "id": "S", "name": "Room Scan", "aligned": True, "items": [
                {"kind": "wall", "transform": column_major(wall), "dimensions": [4.0, 2.7, 0.1]},
                {"kind": "floor", "transform": column_major(Matrix.Identity(4)), "dimensions": [4.0, 0.01, 3.0],
                 "polygon": [[-2.0, 0, -1.5], [2.0, 0, -1.5], [2.0, 0, 1.5], [-2.0, 0, 1.5]]},
            ]})
            # The phone 3 m in front of the origin, 1.6 m up, looking at it.
            app.send({"type": "setCamera", "camera": "Camera", "m": column_major(Matrix.Translation((0.5, 1.6, 3.0)))})
            self.assertTrue(run.wait(lambda: any(n["id"] == "n2" for n in app.extras["notes"])))
            run.wait(lambda: "Wall 1" in bpy.data.objects)
            time.sleep(0.2)
            run.tick()

            note = next(o for o in scene.objects if o.get("viewar_note_id") == "n2")
            self.assertEqual(tuple(round(v, 3) for v in note.location), (500.0, 0.0, 0.0), "500 mm")
            self.assertEqual(next(n for n in app.extras["notes"] if n["id"] == "n2")["position"], [0.5, 0.0, 0.0])

            wall_obj = bpy.data.objects["Wall 1"]
            self.assertEqual(tuple(round(v, 3) for v in wall_obj.location), (0.0, 2000.0, 1350.0))
            self.assertEqual(tuple(round(v, 6) for v in wall_obj.scale), (1.0, 1.0, 1.0), "the box carries its size")
            self.assertEqual(tuple(round(v, 3) for v in wall_obj.dimensions), (4000.0, 100.0, 2700.0))
            floor = bpy.data.objects["Floor"]
            self.assertEqual(tuple(round(v, 3) for v in floor.dimensions[:2]), (4000.0, 3000.0))

            camera = bpy.data.objects["Camera"]
            self.assertEqual(tuple(round(v, 3) for v in camera.location), (500.0, -3000.0, 1600.0))
            self.assertEqual(tuple(round(v, 6) for v in camera.scale), (1.0, 1.0, 1.0))
        finally:
            run.close()

    def test_changing_unit_scale_resyncs(self):
        build_scene(1.0)
        run = Streaming()
        try:
            self.assertTrue(run.synced())
            hellos = run.app.counts["hello"]
            bpy.context.scene.unit_settings.scale_length = 0.001  # the same numbers, now millimeters
            self.assertTrue(run.wait(lambda: run.app.counts["hello"] > hellos))
            time.sleep(0.2)
            run.tick()
            result = summary(run.app)
            self.assertEqual(result["meshes"]["Cube"]["positions"][:3], [-0.001, -0.001, 0.001])
            self.assertEqual(result["matrices"]["Cube"][12], 0.002)
            leg = result["prototypes"][0]
            self.assertAlmostEqual(max(leg), 0.0004, places=6, msg="instanced meshes are rebuilt in meters too")
        finally:
            run.close()


if __name__ == "__main__":
    result = unittest.main(argv=["test_units"], exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
