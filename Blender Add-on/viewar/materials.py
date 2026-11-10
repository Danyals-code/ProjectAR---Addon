# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading Blender materials for the app, and making the ones the app defines.

A material is described by the Principled BSDF that feeds the active Material
Output's Surface. Reroutes, group nodes and the first shader of a Mix or Add
Shader are followed, and inputs fed by an RGB or Value node use that node's
value.

Looks made in the app, and its built-in looks, arrive as definitions with a
stable key. The material made for one keeps that key in the custom property
KEY_PROP, so applying the same look again reuses it, and editing the look in
the app updates it.
"""

import math
import re

import bpy

from . import textures

# Blender 5.0 deprecated Material.use_nodes: node materials are always used.
HAS_USE_NODES = bpy.app.version < (5, 0, 0)

KEY_PROP = "projectar_material_key"
MAX_KEY = 128
MAX_NAME = 63

DEFAULT_MATERIAL = {
    "name": "Default",
    "color": [0.8, 0.8, 0.8, 1.0],
    "metallic": 0.0,
    "roughness": 0.5,
}

# Set by the streamer. Materials list their textures here.
texture_store = None

_MAX_DEPTH = 32


# ---------------------------------------------------------------- colors


def to_srgb(c):
    c = min(max(float(c), 0.0), 1.0)
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def to_linear(c):
    c = min(max(float(c), 0.0), 1.0)
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# ---------------------------------------------------------------- node graph


def _group_output(tree):
    outputs = [n for n in tree.nodes if n.type == "GROUP_OUTPUT"]
    return next((n for n in outputs if n.is_active_output), outputs[0] if outputs else None)


def _by_identifier(sockets, identifier):
    return next((s for s in sockets if s.identifier == identifier), None)


def _resolve(socket, groups, depth=0):
    """What feeds an input socket, looking through reroutes and group
    boundaries. Returns (node, output socket, groups) for a node, (None,
    socket, groups) for an unlinked group input whose value is that socket's,
    or None when nothing usable feeds it. `groups` is the stack of group
    nodes entered, outermost first."""
    if socket is None or depth > _MAX_DEPTH or not socket.is_linked:
        return None
    link = socket.links[0]
    if not link.is_valid or link.is_muted:
        return None
    node, out = link.from_node, link.from_socket
    if node.type == "REROUTE":
        return _resolve(node.inputs[0], groups, depth + 1)
    if node.type == "GROUP":
        tree = node.node_tree
        output = _group_output(tree) if tree is not None else None
        inner = _by_identifier(output.inputs, out.identifier) if output is not None else None
        return _resolve(inner, groups + (node,), depth + 1)
    if node.type == "GROUP_INPUT":
        if not groups:
            return None
        outer = _by_identifier(groups[-1].inputs, out.identifier)
        if outer is None:
            return None
        if not outer.is_linked:
            return None, outer, groups[:-1]
        return _resolve(outer, groups[:-1], depth + 1)
    return node, out, groups


def _shader(socket, groups, depth=0):
    """The Principled BSDF a shader socket comes from, and its group stack."""
    found = _resolve(socket, groups)
    if found is None or found[0] is None or depth > _MAX_DEPTH:
        return None
    node, _, groups = found
    if node.type == "BSDF_PRINCIPLED":
        return node, groups
    if node.type == "MIX_SHADER":
        candidates = node.inputs[1:3]
    elif node.type == "ADD_SHADER":
        candidates = node.inputs[0:2]
    else:
        return None
    for candidate in candidates:
        result = _shader(candidate, groups, depth + 1)
        if result is not None:
            return result
    return None


def _active_output(tree):
    outputs = [n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"]
    for wanted in (("ALL", "EEVEE"), ("ALL", "EEVEE", "CYCLES")):
        for node in outputs:
            if node.is_active_output and node.target in wanted:
                return node
    return outputs[0] if outputs else None


def surface_principled(tree):
    """(Principled BSDF, group stack) for a material's node tree, or None.
    Falls back to the first Principled BSDF in the tree, as older versions did."""
    output = _active_output(tree)
    if output is not None:
        found = _shader(output.inputs.get("Surface"), ())
        if found is not None:
            return found
    node = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    return (node, ()) if node is not None else None


def _as_like(value, fallback):
    """A socket value shaped like the fallback: a float for float inputs,
    four floats for colors."""
    try:
        values = list(value)
    except TypeError:
        values = None
    if isinstance(fallback, (int, float)):
        if values is None:
            return float(value)
        rgb = values[:3]
        return sum(float(v) for v in rgb) / max(len(rgb), 1)
    if values is None:
        v = float(value)
        return [v, v, v, 1.0]
    values = [float(v) for v in values]
    return (values + [1.0, 1.0, 1.0, 1.0])[:4] if len(values) < 4 else values[:4]


def input_value(node, name, fallback, groups=()):
    """An input's value: its own when unlinked, an RGB or Value node's when
    one feeds it, otherwise the fallback."""
    socket = node.inputs.get(name)
    if socket is None:
        return fallback
    if not socket.is_linked:
        return _as_like(socket.default_value, fallback)
    found = _resolve(socket, groups)
    if found is None:
        return fallback
    source, out, _ = found
    if source is None:
        value = getattr(out, "default_value", None)
        return fallback if value is None else _as_like(value, fallback)
    if source.type in ("RGB", "VALUE"):
        return _as_like(out.default_value, fallback)
    return fallback


def node_trees_in(tree, seen=None):
    """Every node group used by a node tree, nested ones included."""
    seen = set() if seen is None else seen
    for node in tree.nodes:
        group = getattr(node, "node_tree", None) if node.type == "GROUP" else None
        if group is not None and group.as_pointer() not in seen:
            seen.add(group.as_pointer())
            yield group
            yield from node_trees_in(group, seen)


def materials_using(tree):
    """Full names of materials whose own node tree is `tree` or uses it as a
    node group, however deeply nested."""
    target = tree.as_pointer()
    names = set()
    for mat in bpy.data.materials:
        own = mat.node_tree
        if own is None:
            continue
        if own.as_pointer() == target or any(g.as_pointer() == target for g in node_trees_in(own)):
            names.add(mat.name_full)
    return names


# ---------------------------------------------------------------- reading


def _uses_nodes(mat):
    return mat.node_tree is not None and (mat.use_nodes if HAS_USE_NODES else True)


def material_info(mat):
    """A material as the app reads it. sRGB color, alpha, metallic,
    roughness, texture ids when textures are streamed, and the app's key for
    materials it made."""
    if mat is None:
        return dict(DEFAULT_MATERIAL)
    color = list(mat.diffuse_color)
    metallic = float(mat.metallic)
    roughness = float(mat.roughness)
    maps = None
    if _uses_nodes(mat):
        found = surface_principled(mat.node_tree)
        if found is not None:
            principled, groups = found
            color = input_value(principled, "Base Color", color, groups)
            metallic = float(input_value(principled, "Metallic", metallic, groups))
            roughness = float(input_value(principled, "Roughness", roughness, groups))
            color[3] = float(input_value(principled, "Alpha", float(color[3]), groups))
            if texture_store is not None:
                found_maps, alpha = textures.material_textures(principled)
                if found_maps:
                    maps = {role: texture_store.register(image, role) for role, image in found_maps.items()}
                    if alpha:
                        maps["alpha"] = True
    info = {
        "name": mat.name,
        "color": [to_srgb(c) for c in color[:3]] + [min(max(float(color[3]), 0.0), 1.0)],
        "metallic": min(max(metallic, 0.0), 1.0),
        "roughness": min(max(roughness, 0.0), 1.0),
    }
    key = mat.get(KEY_PROP)
    if isinstance(key, str) and key:
        info["key"] = key
    if maps:
        # Older apps ignore this and keep the plain color.
        info["textures"] = maps
    return info


def slot_names(obj):
    """The material in each slot, by full name, for noticing reassignments."""
    return tuple(s.material.name_full if s.material is not None else "" for s in obj.material_slots)


# ---------------------------------------------------------------- app looks


def _finite(value, low=0.0, high=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("not finite")
    return min(max(value, low), high)


def _one_line(value, limit):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    return value.encode("utf-8")[:limit].decode("utf-8", "ignore").strip()


def parse_definition(value):
    """A cleaned definition from the app, or None if it isn't usable."""
    if not isinstance(value, dict):
        return None
    key = _one_line(value.get("key"), MAX_KEY)
    name = _one_line(value.get("name"), MAX_NAME)
    color = value.get("color")
    if not key or not name or not isinstance(color, list) or len(color) not in (3, 4):
        return None
    try:
        rgba = [_finite(c) for c in color] + ([1.0] if len(color) == 3 else [])
        metallic = _finite(value.get("metallic", 0.0))
        roughness = _finite(value.get("roughness", 0.5))
    except ValueError:
        return None
    return {"key": key, "name": name, "color": rgba, "metallic": metallic, "roughness": roughness}


def find_app_material(key):
    return next(
        (m for m in bpy.data.materials if m.library is None and m.get(KEY_PROP) == key),
        None,
    )


def _ensure_principled(mat):
    """The material's surface Principled BSDF, made if it has none. Returns
    the node and whether the tree had to change."""
    if HAS_USE_NODES and not mat.use_nodes:
        mat.use_nodes = True
    tree = mat.node_tree
    if tree is None:
        return None, False
    found = surface_principled(tree)
    if found is not None and not found[1]:  # one inside a group isn't ours to edit
        return found[0], False
    principled = tree.nodes.new("ShaderNodeBsdfPrincipled")
    output = _active_output(tree) or tree.nodes.new("ShaderNodeOutputMaterial")
    principled.location = (output.location.x - 300, output.location.y)
    tree.links.new(principled.outputs[0], output.inputs["Surface"])
    return principled, True


def _close(a, b):
    return abs(float(a) - float(b)) < 1e-4


def _set_input(node, name, value):
    socket = node.inputs.get(name)
    if socket is None or socket.is_linked:
        return False
    current = socket.default_value
    try:
        same = all(_close(x, y) for x, y in zip(current, value))
    except TypeError:
        same = _close(current, value)
    if same:
        return False
    socket.default_value = value
    return True


def _set_blending(mat, transparent):
    changed = False
    if hasattr(mat, "surface_render_method"):  # EEVEE Next, Blender 4.2 and later
        wanted = "BLENDED" if transparent else "DITHERED"
        if mat.surface_render_method != wanted:
            mat.surface_render_method = wanted
            changed = True
    elif hasattr(mat, "blend_method"):
        wanted = "BLEND" if transparent else "OPAQUE"
        if mat.blend_method != wanted:
            mat.blend_method = wanted
            changed = True
    return changed


def _base_name(name):
    return re.sub(r"\.\d{3}$", "", name)


def apply_definition(mat, definition):
    """Sets a material's values from a definition. Returns True if anything changed."""
    linear = [to_linear(c) for c in definition["color"][:3]]
    alpha = definition["color"][3]
    changed = False
    principled, added = _ensure_principled(mat)
    changed |= added
    if principled is not None:
        changed |= _set_input(principled, "Base Color", linear + [1.0])
        changed |= _set_input(principled, "Metallic", definition["metallic"])
        changed |= _set_input(principled, "Roughness", definition["roughness"])
        changed |= _set_input(principled, "Alpha", alpha)
    display = linear + [alpha]
    if not all(_close(a, b) for a, b in zip(mat.diffuse_color, display)):
        mat.diffuse_color = display
        changed = True
    if not _close(mat.metallic, definition["metallic"]):
        mat.metallic = definition["metallic"]
        changed = True
    if not _close(mat.roughness, definition["roughness"]):
        mat.roughness = definition["roughness"]
        changed = True
    changed |= _set_blending(mat, alpha < 0.999)
    if _base_name(mat.name) != definition["name"]:
        mat.name = definition["name"]
        changed = True
    return changed


def ensure_app_material(definition):
    """The material for a look from the app, made or updated. Returns
    (material, changed)."""
    mat = find_app_material(definition["key"])
    created = mat is None
    if created:
        mat = bpy.data.materials.new(definition["name"])
        mat[KEY_PROP] = definition["key"]
    changed = apply_definition(mat, definition)
    return mat, created or changed
