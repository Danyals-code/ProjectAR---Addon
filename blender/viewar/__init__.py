# SPDX-License-Identifier: GPL-3.0-or-later
import traceback

import bpy
from bpy.app.handlers import persistent

from . import discovery, review, scene_stream, server

HEAVY_SCENE_TRIANGLES = 1_000_000

_state = {
    "server": None,
    "streamer": None,
    "advertiser": None,
    "addresses": [],
    "error": "",
    "last_ui": None,
}


def _prefs(context=None):
    addon = (context or bpy.context).preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


def _options():
    context = bpy.context
    prefs = _prefs(context)
    session = context.window_manager.viewar
    return scene_stream.Options(
        max_triangles=prefs.max_triangles,
        include_instances=session.include_instances,
        max_instances=prefs.max_instances,
        scope=session.scope,
        collection=session.collection,
        origin=session.origin,
        paused=session.paused,
    )


# ---------------------------------------------------------------- handlers


@persistent
def _on_depsgraph_update(scene, depsgraph):
    streamer = _state["streamer"]
    if streamer is not None:
        streamer.on_depsgraph_update(scene, depsgraph)


@persistent
def _on_load_post(*_args):
    streamer = _state["streamer"]
    if streamer is not None:
        streamer.request_full_sync()


def _tick():
    streamer = _state["streamer"]
    if streamer is None:
        return None  # unregisters the timer
    try:
        streamer.tick()
    except Exception as exc:
        _state["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    _redraw_if_changed(streamer)
    prefs = _prefs()
    return 1.0 / max(prefs.update_rate if prefs is not None else 30, 1)


def _redraw_if_changed(streamer):
    snapshot = (
        # bytes >> 17: redraw about every 128 KB so "MB sent" stays current
        tuple((c.label, c.bytes_sent >> 17) for c in streamer.clients),
        streamer.object_count,
        streamer.instance_count,
        streamer.instances_capped,
        streamer.triangle_count,
        len(streamer.skipped),
        _state["error"],
    )
    if snapshot == _state["last_ui"]:
        return
    _state["last_ui"] = snapshot
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _auto_start():
    prefs = _prefs()
    if prefs is not None and prefs.auto_start and _state["server"] is None:
        try:
            start(bpy.context)
        except OSError as exc:
            stop()
            _state["error"] = f"Auto start failed: {exc}"
            print(f"ProjectAR: {_state['error']}")
    return None


# ---------------------------------------------------------------- lifecycle


def start(context):
    prefs = _prefs(context)
    srv = server.Server(prefs.port)
    srv.start()  # raises OSError if the port is taken
    _state["server"] = srv
    _state["error"] = ""
    _state["addresses"] = discovery.local_addresses()
    _state["streamer"] = scene_stream.SceneStreamer(srv, _options)
    advertiser = discovery.Advertiser()
    advertiser.start(prefs.port, _state["addresses"])
    _state["advertiser"] = advertiser

    handlers = bpy.app.handlers
    if _on_depsgraph_update not in handlers.depsgraph_update_post:
        handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_post not in handlers.load_post:
        handlers.load_post.append(_on_load_post)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.1, persistent=True)


def stop():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    handlers = bpy.app.handlers
    if _on_depsgraph_update in handlers.depsgraph_update_post:
        handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _on_load_post in handlers.load_post:
        handlers.load_post.remove(_on_load_post)
    if _state["advertiser"] is not None:
        _state["advertiser"].stop()
    if _state["server"] is not None:
        _state["server"].stop()
    _state.update(server=None, streamer=None, advertiser=None, addresses=[], last_ui=None)


# ---------------------------------------------------------------- settings


class ViewARPreferences(bpy.types.AddonPreferences):
    """Kept in Blender's preferences, so they survive restarts."""

    bl_idname = __package__

    port: bpy.props.IntProperty(
        name="Port",
        default=51515,
        min=1024,
        max=65535,
        description="TCP port the ProjectAR app connects to",
    )
    update_rate: bpy.props.IntProperty(
        name="Updates per Second",
        default=30,
        min=5,
        max=60,
        description="How often changes are sent. Lower it for heavy animated scenes",
    )
    max_triangles: bpy.props.IntProperty(
        name="Max Triangles per Object",
        default=300_000,
        min=1_000,
        max=5_000_000,
        description="Objects above this are not sent, to keep older iPhones smooth",
    )
    max_instances: bpy.props.IntProperty(
        name="Max Instances",
        default=500,
        min=0,
        max=10_000,
        description="Instances beyond this are not sent. Each instance is streamed as its own mesh",
    )
    auto_start: bpy.props.BoolProperty(
        name="Start Server with Blender",
        default=False,
        description="Start streaming automatically whenever Blender opens",
    )

    def draw(self, context):
        _draw_settings(self.layout, self)


class ViewARSession(bpy.types.PropertyGroup):
    """Per-session choices. Not saved into .blend files."""

    scope: bpy.props.EnumProperty(
        name="Stream",
        items=(
            ("VISIBLE", "Visible Objects", "Everything visible in the current view layer"),
            ("SELECTED", "Selected Objects", "Only selected objects, handy for checking one asset"),
            ("COLLECTION", "Collection", "Only objects in one collection"),
        ),
        default="VISIBLE",
    )
    collection: bpy.props.StringProperty(
        name="Collection", description="Collection to stream"
    )
    origin: bpy.props.EnumProperty(
        name="AR Origin",
        items=(
            ("WORLD", "World Origin", "Blender's world origin lands where you tap in the app"),
            ("CURSOR", "3D Cursor", "The 3D cursor lands where you tap, so models far from the origin are easy to place"),
        ),
        default="WORLD",
    )
    include_instances: bpy.props.BoolProperty(
        name="Include Instances",
        default=True,
        description="Stream collection, particle and Geometry Nodes instances",
    )
    paused: bpy.props.BoolProperty(
        name="Pause Updates",
        default=False,
        description="Freeze what devices show while you make heavy edits. Changes are sent when you resume",
    )


def _draw_settings(layout, prefs):
    col = layout.column()
    sub = col.column()
    sub.enabled = _state["server"] is None
    sub.prop(prefs, "port")
    col.prop(prefs, "update_rate")
    col.prop(prefs, "max_triangles")
    col.prop(prefs, "max_instances")
    col.prop(prefs, "auto_start")


# ---------------------------------------------------------------- operators


class VIEWAR_OT_start(bpy.types.Operator):
    bl_idname = "viewar.start"
    bl_label = "Start Server"
    bl_description = "Stream this scene to the ProjectAR app on your local network"

    def execute(self, context):
        if _state["server"] is not None:
            return {"CANCELLED"}
        try:
            start(context)
        except OSError as exc:
            stop()
            _state["error"] = f"Could not open port: {exc}"
            self.report({"ERROR"}, _state["error"])
            return {"CANCELLED"}
        return {"FINISHED"}


class VIEWAR_OT_stop(bpy.types.Operator):
    bl_idname = "viewar.stop"
    bl_label = "Stop"
    bl_description = "Stop streaming and disconnect all devices"

    def execute(self, context):
        stop()
        return {"FINISHED"}


class VIEWAR_OT_resync(bpy.types.Operator):
    bl_idname = "viewar.resync"
    bl_label = "Resync"
    bl_description = "Send the whole scene to all connected devices again"

    def execute(self, context):
        if _state["streamer"] is not None:
            _state["streamer"].request_full_sync()
        return {"FINISHED"}


class VIEWAR_OT_copy_address(bpy.types.Operator):
    bl_idname = "viewar.copy_address"
    bl_label = "Copy Address"
    bl_description = (
        "Copy this computer's address and port. With Universal Clipboard "
        "you can paste it straight into the app on your iPhone"
    )

    def execute(self, context):
        srv = _state["server"]
        if srv is None or not _state["addresses"]:
            return {"CANCELLED"}
        text = f"{_state['addresses'][0]}:{srv.port}"
        context.window_manager.clipboard = text
        self.report({"INFO"}, f"Copied {text}")
        return {"FINISHED"}


class VIEWAR_OT_disconnect(bpy.types.Operator):
    bl_idname = "viewar.disconnect"
    bl_label = "Disconnect Device"
    bl_description = "Disconnect this device. It can connect again from the app"

    device: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if _state["server"] is not None:
            _state["server"].disconnect(self.device)
        return {"FINISHED"}


class VIEWAR_OT_mark_variant_set(bpy.types.Operator):
    bl_idname = "viewar.mark_variant_set"
    bl_label = "Use Active Collection as Variant Set"
    bl_description = (
        "Treat each child collection of the active collection as a design "
        "option you can switch between in the app"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        layer = context.view_layer.active_layer_collection
        return layer is not None and len(layer.collection.children) >= 2

    def execute(self, context):
        coll = context.view_layer.active_layer_collection.collection
        coll.viewar_variant_set = True
        # Start with one option showing, so Blender and the app agree.
        first = coll.children[0].name
        layers = review.layer_collections(context.view_layer)
        if not any(
            not layers[c.name].exclude and not layers[c.name].hide_viewport
            for c in coll.children
            if c.name in layers
        ):
            review.apply_variant(context.view_layer, coll.name, first)
        return {"FINISHED"}


class VIEWAR_OT_unmark_variant_set(bpy.types.Operator):
    bl_idname = "viewar.unmark_variant_set"
    bl_label = "Stop Using as Variant Set"
    bl_description = "Stream this collection as ordinary objects again"
    bl_options = {"REGISTER", "UNDO"}

    collection: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        coll = bpy.data.collections.get(self.collection)
        if coll is None:
            return {"CANCELLED"}
        coll.viewar_variant_set = False
        return {"FINISHED"}


class VIEWAR_OT_set_variant(bpy.types.Operator):
    bl_idname = "viewar.set_variant"
    bl_label = "Show Option"
    bl_description = "Show this option and hide the others, here and in the app"
    bl_options = {"REGISTER", "UNDO"}

    collection: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    option: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if not review.apply_variant(context.view_layer, self.collection, self.option):
            return {"CANCELLED"}
        return {"FINISHED"}


class VIEWAR_OT_add_note(bpy.types.Operator):
    bl_idname = "viewar.add_note"
    bl_label = "Add Note at 3D Cursor"
    bl_description = "Leave a note at the 3D cursor. It shows up as a pin in the app"
    bl_options = {"REGISTER", "UNDO"}

    text: bpy.props.StringProperty(name="Note", maxlen=review.MAX_NOTE_LENGTH)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def execute(self, context):
        if not self.text.strip():
            return {"CANCELLED"}
        scene = context.scene
        review.add_note(scene, scene.cursor.location.copy(), self.text.strip(), "Blender")
        return {"FINISHED"}


class VIEWAR_OT_remove_note(bpy.types.Operator):
    bl_idname = "viewar.remove_note"
    bl_label = "Delete Note"
    bl_description = "Delete this note here and in the app"
    bl_options = {"REGISTER", "UNDO"}

    note_id: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if not review.remove_note(context.scene, self.note_id):
            return {"CANCELLED"}
        return {"FINISHED"}


class VIEWAR_OT_select_note(bpy.types.Operator):
    bl_idname = "viewar.select_note"
    bl_label = "Select Note"
    bl_description = "Select this note's empty in the viewport"

    note_id: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        obj = review.find_note(context.scene, self.note_id)
        if obj is None or not obj.visible_get():
            return {"CANCELLED"}
        for other in context.selected_objects:
            other.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


# ---------------------------------------------------------------- panels


class VIEWAR_PT_main(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_main"
    bl_label = "ProjectAR"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"

    def draw(self, context):
        layout = self.layout
        srv = _state["server"]

        if _state["error"]:
            box = layout.box()
            box.label(text=_state["error"], icon="ERROR")
            box.label(text="Details are in the system console.")

        if srv is None:
            layout.operator(VIEWAR_OT_start.bl_idname, icon="PLAY")
            layout.label(text=f"Port {_prefs(context).port}")
            if not discovery.AVAILABLE:
                layout.label(text="Auto discovery unavailable.", icon="INFO")
                layout.label(text="Enter this computer's IP in the app.")
            return

        streamer = _state["streamer"]
        advertiser = _state["advertiser"]
        session = context.window_manager.viewar
        addresses = _state["addresses"]

        box = layout.box()
        row = box.row()
        row.label(text=f"{addresses[0]}:{srv.port}", icon="URL")
        row.operator(VIEWAR_OT_copy_address.bl_idname, text="", icon="COPYDOWN")
        for address in addresses[1:3]:
            box.label(text=f"or {address}:{srv.port}")
        if advertiser is not None and advertiser.active:
            box.label(text="Visible to nearby devices", icon="CHECKMARK")
        else:
            box.label(text="Use manual IP in the app", icon="INFO")

        clients = streamer.clients
        box = layout.box()
        box.label(
            text=f"Devices: {len(clients)}",
            icon="LINKED" if clients else "UNLINKED",
        )
        for client in clients:
            row = box.row()
            row.label(text=client.label)
            row.label(text=f"{client.bytes_sent / 1e6:.1f} MB")
            op = row.operator(VIEWAR_OT_disconnect.bl_idname, text="", icon="X")
            op.device = client.label

        layout.prop(
            session,
            "paused",
            text="Resume Updates" if session.paused else "Pause Updates",
            icon="PLAY" if session.paused else "PAUSE",
            toggle=True,
        )
        row = layout.row(align=True)
        row.operator(VIEWAR_OT_resync.bl_idname, icon="FILE_REFRESH")
        row.operator(VIEWAR_OT_stop.bl_idname, icon="CANCEL")


class VIEWAR_PT_streaming(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_streaming"
    bl_label = "Streaming"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"

    def draw(self, context):
        layout = self.layout
        session = context.window_manager.viewar

        col = layout.column()
        col.prop(session, "scope", text="")
        if session.scope == "COLLECTION":
            col.prop_search(session, "collection", bpy.data, "collections", text="")
            if session.collection not in bpy.data.collections:
                col.label(text="Pick a collection to stream", icon="INFO")
        col.prop(session, "origin")
        col.prop(session, "include_instances")

        obj = context.active_object
        if obj is not None:
            layout.prop(obj, "viewar_visible", text=f"Show {obj.name} in AR")


class VIEWAR_PT_variants(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_variants"
    bl_label = "Variants"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        sets = review.variant_sets(context.scene, context.view_layer)
        if not sets:
            col = layout.column(align=True)
            col.label(text="Put each design option in its own")
            col.label(text="collection inside a parent collection.")
            col.label(text="Select the parent in the Outliner, then:")
        for variant_set in sets:
            box = layout.box()
            row = box.row()
            row.label(text=variant_set["name"], icon="OUTLINER_COLLECTION")
            op = row.operator(VIEWAR_OT_unmark_variant_set.bl_idname, text="", icon="X", emboss=False)
            op.collection = variant_set["name"]
            col = box.column(align=True)
            for option in variant_set["options"]:
                active = option["name"] == variant_set["active"]
                op = col.operator(
                    VIEWAR_OT_set_variant.bl_idname,
                    text=option["name"],
                    icon="RADIOBUT_ON" if active else "RADIOBUT_OFF",
                    depress=active,
                )
                op.collection = variant_set["name"]
                op.option = option["name"]
        layout.operator(VIEWAR_OT_mark_variant_set.bl_idname, icon="ADD")


class VIEWAR_PT_notes(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_notes"
    bl_label = "Notes"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        notes = review.note_objects(context.scene)
        if not notes:
            layout.label(text="Notes left in the app appear here.")
        active = context.active_object
        for obj in notes:
            note_id = obj["viewar_note_id"]
            box = layout.box()
            row = box.row()
            op = row.operator(
                VIEWAR_OT_select_note.bl_idname,
                text="",
                icon="RESTRICT_SELECT_OFF",
                emboss=False,
            )
            op.note_id = note_id
            if obj == active:
                row.prop(obj, '["viewar_note_text"]', text="")
            else:
                row.label(text=str(obj.get("viewar_note_text", "")))
            op = row.operator(VIEWAR_OT_remove_note.bl_idname, text="", icon="X", emboss=False)
            op.note_id = note_id
            author = obj.get("viewar_note_author", "")
            if author:
                box.label(text=str(author), icon="USER")
        layout.operator(VIEWAR_OT_add_note.bl_idname, icon="ADD")


class VIEWAR_PT_stats(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_stats"
    bl_label = "Statistics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"

    @classmethod
    def poll(cls, context):
        return _state["streamer"] is not None

    def draw(self, context):
        layout = self.layout
        streamer = _state["streamer"]
        if not streamer.clients:
            layout.label(text="Connect a device to see statistics.")
            return
        col = layout.column()
        col.label(text=f"Objects: {streamer.object_count}")
        col.label(text=f"Instances: {streamer.instance_count}")
        if streamer.instances_capped:
            col.label(text="Instance limit reached", icon="ERROR")
        col.label(text=f"Triangles: {streamer.triangle_count:,}")
        if streamer.triangle_count > HEAVY_SCENE_TRIANGLES:
            col.label(text="Heavy for older iPhones", icon="ERROR")
        for name in sorted(streamer.skipped)[:5]:
            col.label(text=f"Skipped: {name}", icon="ERROR")
        if len(streamer.skipped) > 5:
            col.label(text=f"...and {len(streamer.skipped) - 5} more skipped")


class VIEWAR_PT_settings(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_settings"
    bl_label = "Settings"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        _draw_settings(self.layout, _prefs(context))


classes = (
    ViewARPreferences,
    ViewARSession,
    VIEWAR_OT_start,
    VIEWAR_OT_stop,
    VIEWAR_OT_resync,
    VIEWAR_OT_copy_address,
    VIEWAR_OT_disconnect,
    VIEWAR_OT_mark_variant_set,
    VIEWAR_OT_unmark_variant_set,
    VIEWAR_OT_set_variant,
    VIEWAR_OT_add_note,
    VIEWAR_OT_remove_note,
    VIEWAR_OT_select_note,
    VIEWAR_PT_main,
    VIEWAR_PT_streaming,
    VIEWAR_PT_variants,
    VIEWAR_PT_notes,
    VIEWAR_PT_stats,
    VIEWAR_PT_settings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.viewar = bpy.props.PointerProperty(type=ViewARSession)
    bpy.types.Object.viewar_visible = bpy.props.BoolProperty(
        name="Show in AR",
        default=True,
        description="Stream this object to ProjectAR. Alt+click to change all selected objects",
    )
    bpy.types.Collection.viewar_variant_set = bpy.props.BoolProperty(
        name="AR Variant Set",
        default=False,
        description="Each child collection is a design option you can switch between in the app",
    )
    if not bpy.app.background:
        bpy.app.timers.register(_auto_start, first_interval=1.0)


def unregister():
    if bpy.app.timers.is_registered(_auto_start):
        bpy.app.timers.unregister(_auto_start)
    stop()
    del bpy.types.Collection.viewar_variant_set
    del bpy.types.Object.viewar_visible
    del bpy.types.WindowManager.viewar
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
