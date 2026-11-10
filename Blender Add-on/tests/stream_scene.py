# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds a small scene in Blender and streams it to a fake ProjectAR app,
returning what the app ended up with. Used by test_units.py; runs inside
Blender (blender -b --factory-startup --python ...).

The fake app is the Rhino add-on's (Rhino Add-on/tests/fake_app.py): both
add-ons speak the same protocol, so one stand-in for the app serves both.
"""

import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "Rhino Add-on", "tests"))

import bpy  # noqa: E402

from viewar import review, scene_stream, server  # noqa: E402
from fake_app import FakeApp  # noqa: E402

# The properties the add-on registers when it's enabled; the test uses its modules directly.
if not hasattr(bpy.types.Object, "viewar_visible"):
    bpy.types.Object.viewar_visible = bpy.props.BoolProperty(default=True)
    bpy.types.Collection.viewar_variant_set = bpy.props.BoolProperty(default=False)


class LocalServer(server.Server):
    """The add-on's server, listening on 127.0.0.1 only, so a test never
    opens a port to the network."""

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.listen(4)
        sock.settimeout(0.2)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_scene(unit_scale):
    """A cube 2 m wide 2 m along X, a collection instance, a lamp 3 m up, a
    camera and a note, all at the same real size whatever the Unit Scale."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.scale_length = unit_scale
    bu = 1.0 / unit_scale  # Blender units per meter

    mesh = bpy.data.meshes.new("Cube")
    s = 1.0 * bu
    mesh.from_pydata([(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s), (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)], [],
                     [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])
    cube = bpy.data.objects.new("Cube", mesh)
    cube.location = (2.0 * bu, 0, 0)
    scene.collection.objects.link(cube)

    leg_mesh = bpy.data.meshes.new("Leg")
    leg_mesh.from_pydata([(0, 0, 0), (0.05 * bu, 0, 0), (0, 0, 0.4 * bu)], [], [(0, 1, 2)])
    parts = bpy.data.collections.new("Parts")
    parts.objects.link(bpy.data.objects.new("Leg", leg_mesh))
    for i, x in enumerate((-2.0, -3.0)):
        empty = bpy.data.objects.new(f"Stool {i + 1}", None)
        empty.instance_type = "COLLECTION"
        empty.instance_collection = parts
        empty.location = (x * bu, 0, 0)
        scene.collection.objects.link(empty)

    lamp_data = bpy.data.lights.new("Lamp", "POINT")
    # Blender's renders measure light falloff in Blender units, so a scene in
    # millimeters needs a million times the watts to look the same.
    lamp_data.energy = 1000.0 * bu * bu
    lamp_data.shadow_soft_size = 0.25 * bu
    lamp = bpy.data.objects.new("Lamp", lamp_data)
    lamp.location = (0, 0, 3.0 * bu)
    scene.collection.objects.link(lamp)
    panel_data = bpy.data.lights.new("Panel", "AREA")
    panel_data.size = 0.5 * bu
    panel_data.energy = 100.0 * bu * bu
    panel = bpy.data.objects.new("Panel", panel_data)
    panel.location = (0, 0, 2.5 * bu)
    scene.collection.objects.link(panel)

    camera_data = bpy.data.cameras.new("Camera")
    camera_data.clip_start, camera_data.clip_end = 0.1 * bu, 100.0 * bu
    camera = bpy.data.objects.new("Camera", camera_data)
    camera.location = (0, -6.0 * bu, 1.6 * bu)
    camera.rotation_euler = (1.5708, 0, 0)
    scene.collection.objects.link(camera)
    scene.camera = camera

    review.add_note(scene, (1.0 * bu, 0, 0), "Too tall", "Ana", note_id="n1", created="2026-11-11T10:00:00Z")
    bpy.context.view_layer.update()
    return scene


class Streaming:
    """A streamer for the current scene, a server on 127.0.0.1 and a fake app."""

    def __init__(self, features=("textures", "materialUpdates", "instancing", "snapshot")):
        self.port = free_port()
        self.server = LocalServer(self.port)
        self.server.start()
        self.streamer = scene_stream.SceneStreamer(self.server, lambda: scene_stream.Options(require_approval=False))
        self.app = FakeApp(self.port, features=features)

    def tick(self):
        self.streamer.tick()

    def wait(self, condition, timeout=5.0):
        return self.app.wait(condition, timeout=timeout, tick=self.tick)

    def synced(self):
        return self.wait(lambda: self.app.hello is not None and "cameras" in self.app.extras)

    def close(self):
        self.app.close()
        self.server.stop()


def summary(app):
    """What the app has, rounded, for comparing runs."""
    def rounded(values):
        return [round(v, 4) for v in values]

    with app.lock:
        meshes = {k: {"positions": rounded(m["positions"]), "indices": list(m["indices"])} for k, m in app.meshes.items()}
        prototypes = {k: rounded(m["positions"]) for k, m in app.prototypes.items()}
        return {
            "unitScale": app.hello["unitScale"],
            "meshes": meshes,
            "prototypes": sorted(prototypes.values()),
            "matrices": {k: rounded(v) for k, v in app.matrices.items()},
            "instances": sorted(app.instances),
            "lights": app.extras["lights"],
            "cameras": app.extras["cameras"],
            "notes": app.extras["notes"],
        }


def collect(unit_scale):
    build_scene(unit_scale)
    run = Streaming()
    try:
        assert run.synced(), "no full sync"
        time.sleep(0.2)
        run.tick()
        return summary(run.app)
    finally:
        run.close()


if __name__ == "__main__":
    # Writes what the app gets, for comparing before and after a change:
    #   blender -b --factory-startup --python stream_scene.py -- out.json
    import json

    out = sys.argv[sys.argv.index("--") + 1]
    with open(out, "w") as f:
        json.dump({str(k): collect(k) for k in (1.0, 0.001)}, f, indent=1, sort_keys=True)
    print(f"wrote {out}")
