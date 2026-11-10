# SPDX-License-Identifier: GPL-3.0-or-later
"""Lights and cameras for the app, and moving a camera to where a device is.

Light and camera matrices are in RealityKit space, like "transforms". There a
light shines along its local -Y, and a camera looks along local -Y with local
-Z up (Blender's local -Z and +Y). The app turns them -90 degrees about local
X to get RealityKit's convention.

Everything goes to the app in meters, whatever the Unit Scale (see
scene_stream). Light energies are scaled with it: Blender's renders measure
falloff in Blender units, so in a millimeter scene a light needs a million
times the watts, and the app, measuring in meters, must get a millionth.
"""

import math

import bpy
from mathutils import Matrix

from .materials import to_srgb

# Blender (x, y, z) -> RealityKit (x, z, -y). See protocol section 4.4.
AXIS = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))
AXIS_INV = AXIS.inverted()
# Undoes the app's -90 degree turn about local X.
RX_90 = Matrix.Rotation(math.radians(90.0), 4, "X")

CAMERA_NAME = "ProjectAR Camera"
MAX_LIGHTS = 64
MAX_CAMERAS = 64


def _flatten(m):
    return [round(m[row][col], 6) for col in range(4) for row in range(4)]


def meters(scene):
    """Meters per Blender unit: the scene's Unit Scale."""
    value = float(scene.unit_settings.scale_length)
    return value if math.isfinite(value) and value > 0 else 1.0


def place(space, world, unit):
    """A matrix for the app from a Blender world matrix. space already turns
    Blender units into meters; meshes are scaled to meters too, so that
    scale is taken out here and the app gets rotation and translation only."""
    return _flatten(space @ world @ AXIS_INV @ Matrix.Scale(1.0 / unit, 4))


def _visible(obj, view_layer):
    try:
        return obj.visible_get(view_layer=view_layer)
    except RuntimeError:
        return False


def _light(obj, space, unit):
    data = obj.data
    kind = data.type.lower()
    # Sun strength is per square meter, so it doesn't fall off with distance.
    energy = float(data.energy) * (1.0 if kind == "sun" else unit * unit)
    info = {
        "id": obj.name_full,
        "kind": kind,
        "color": [round(to_srgb(c), 5) for c in data.color],
        "energy": round(energy, 9),
        "shadow": bool(getattr(data, "use_shadow", True)),
        "m": place(space, obj.matrix_world, unit),
    }
    if kind in ("point", "spot"):
        info["radius"] = round(float(data.shadow_soft_size) * unit, 5)
    if kind == "spot":
        info["spotSize"] = round(float(data.spot_size), 5)
        info["spotBlend"] = round(float(data.spot_blend), 5)
    if kind == "area":
        square = data.shape in ("SQUARE", "DISK")
        info["size"] = [round(float(data.size) * unit, 5), round(float(data.size if square else data.size_y) * unit, 5)]
    return info


def render_aspect(scene):
    render = scene.render
    width = render.resolution_x * render.pixel_aspect_x
    height = render.resolution_y * render.pixel_aspect_y
    return width / height if height > 0 else 1.0


def vertical_fov(camera, aspect):
    """The vertical field of view in radians for a camera at an aspect ratio,
    following Blender's sensor fit."""
    if camera.type != "PERSP" or camera.lens <= 0:
        return None
    fit = camera.sensor_fit
    if fit == "AUTO":
        # Auto fits the sensor width to the longer side.
        if aspect >= 1.0:
            horizontal = 2.0 * math.atan(camera.sensor_width / (2.0 * camera.lens))
            return 2.0 * math.atan(math.tan(horizontal / 2.0) / aspect)
        return 2.0 * math.atan(camera.sensor_width / (2.0 * camera.lens))
    if fit == "HORIZONTAL":
        horizontal = 2.0 * math.atan(camera.sensor_width / (2.0 * camera.lens))
        return 2.0 * math.atan(math.tan(horizontal / 2.0) / aspect)
    return 2.0 * math.atan(camera.sensor_height / (2.0 * camera.lens))


def _camera(obj, scene, space, aspect, unit):
    data = obj.data
    info = {
        "id": obj.name_full,
        "active": scene.camera == obj,
        "aspect": round(aspect, 5),
        "clip": [round(float(data.clip_start) * unit, 6), round(float(data.clip_end) * unit, 5)],
        "m": place(space, obj.matrix_world, unit),
    }
    fov = vertical_fov(data, aspect)
    if fov is not None:
        info["fovY"] = round(fov, 6)
        info["projection"] = "perspective"
    elif data.type == "ORTHO":
        info["projection"] = "orthographic"
        info["orthoScale"] = round(float(data.ortho_scale) * unit, 5)
    else:
        info["projection"] = data.type.lower()
    return info


def lights_and_cameras(scene, view_layer, accept, space):
    """Streamed lights and cameras: visible, in scope and shown in AR."""
    lights, cameras = [], []
    aspect = render_aspect(scene)
    unit = meters(scene)
    for obj in scene.objects:
        if obj.type not in ("LIGHT", "CAMERA") or not accept(obj) or not _visible(obj, view_layer):
            continue
        if obj.type == "LIGHT" and len(lights) < MAX_LIGHTS:
            lights.append(_light(obj, space, unit))
        elif obj.type == "CAMERA" and len(cameras) < MAX_CAMERAS:
            cameras.append(_camera(obj, scene, space, aspect, unit))
    lights.sort(key=lambda item: item["id"])
    cameras.sort(key=lambda item: item["id"])
    return lights, cameras


# ---------------------------------------------------------------- setCamera


def _matrix(values):
    """A column-major 4x4 matrix of 16 finite floats, or None."""
    if not isinstance(values, list) or len(values) != 16:
        return None
    floats = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1e7:
            return None
        floats.append(float(v))
    return Matrix([[floats[col * 4 + row] for col in range(4)] for row in range(4)])


def device_pose_to_blender(m, space):
    """A device camera pose (scene space in meters, RealityKit camera axes) as
    a Blender world matrix. space turns meters back into Blender units, which
    also scales the result; set_camera keeps only its location and rotation."""
    return space.inverted_safe() @ m @ RX_90 @ AXIS


def _target_camera(scene, name):
    obj = None
    if isinstance(name, str) and name:
        obj = bpy.data.objects.get(name)
        if obj is not None and (obj.name_full != name or obj.type != "CAMERA"):
            obj = next((o for o in bpy.data.objects if o.name_full == name and o.type == "CAMERA"), None)
    if obj is None and scene.camera is not None and scene.camera.type == "CAMERA":
        obj = scene.camera
    if obj is not None and obj.library is None:
        return obj
    # A linked camera can't be moved, so use (or make) ProjectAR's own.
    own = bpy.data.objects.get(CAMERA_NAME)
    if own is not None and own.type == "CAMERA" and own.library is None:
        if own.name not in scene.objects:
            scene.collection.objects.link(own)
        return own
    data = bpy.data.cameras.new(CAMERA_NAME)
    obj = bpy.data.objects.new(CAMERA_NAME, data)
    scene.collection.objects.link(obj)
    if scene.camera is None:
        scene.camera = obj
    return obj


def set_camera(scene, message, space):
    """Moves a camera to the device's pose. Returns True if it changed."""
    m = _matrix(message.get("m"))
    if m is None:
        return False
    rotation = m.to_3x3()
    try:
        rotation = rotation.normalized()
    except ValueError:
        return False
    if abs(rotation.determinant()) < 1e-3:
        return False
    pose = device_pose_to_blender(m, space)
    obj = _target_camera(scene, message.get("camera"))
    location, orientation, _ = pose.decompose()
    scale = obj.matrix_world.to_scale()
    new = Matrix.LocRotScale(location, orientation, scale)
    if all(abs(a - b) < 1e-6 for row_a, row_b in zip(obj.matrix_world, new) for a, b in zip(row_a, row_b)):
        return False
    obj.matrix_world = new
    return True
