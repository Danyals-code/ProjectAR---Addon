# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import tempfile
import time
import traceback

import bpy
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper

from . import discovery, review, scene_stream, server, study

HEAVY_SCENE_TRIANGLES = 1_000_000

_state = {
    "asking": [],
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
        max_texture_size=int(prefs.max_texture_size),
        require_approval=prefs.require_approval,
        trusted=frozenset(_trusted_devices(prefs)),
    )


def _trusted_devices(prefs):
    """Allowed devices as {device key: name}, from the preferences."""
    try:
        value = json.loads(prefs.trusted_devices or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _set_trusted_devices(prefs, devices):
    prefs.trusted_devices = json.dumps(devices, ensure_ascii=False, sort_keys=True)


# Seconds a request must have been showing before Allow counts.
ANSWER_DELAY = 1.0


def _on_device_request(client):
    """A device asks to connect: say so where it can't be missed."""
    _state["asking"].append(client.label)
    if not bpy.app.background and not bpy.app.timers.is_registered(_show_requests):
        bpy.app.timers.register(_show_requests, first_interval=0.05)


def _show_requests():
    streamer = _state["streamer"]
    labels, _state["asking"] = _state["asking"], []
    if streamer is None:
        return None
    waiting = [c for c in streamer.requests if c.label in labels]
    wm = bpy.context.window_manager
    if not waiting or not wm.windows:
        return None

    def draw(menu, _context):
        # Only text: Blender opens popups under the mouse, so a button here
        # could be clicked by accident and let a device in.
        layout = menu.layout
        for client in waiting:
            layout.label(text=f"{client.display_name} wants to see and change this scene.")
        layout.label(text="Allow or deny it in the ProjectAR panel (press N).")

    try:
        with bpy.context.temp_override(window=wm.windows[0]):
            wm.popup_menu(draw, title="Allow a Device to Connect?", icon="QUESTION")
    except Exception as exc:  # the panel still asks
        print(f"ProjectAR: could not show the connection request: {exc}")
    return None


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
        _state["error"] = ""  # a one-off error shouldn't stay up once things work again
    except Exception as exc:
        _state["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    _redraw_if_changed(streamer)
    prefs = _prefs()
    return 1.0 / max(prefs.update_rate if prefs is not None else 30, 1)


def _redraw_if_changed(streamer):
    snapshot = (
        # bytes >> 17: redraw about every 128 KB so "MB sent" stays current
        tuple((c.label, c.approval, c.bytes_sent >> 17) for c in streamer.clients),
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
    _state["streamer"].on_request = _on_device_request
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
    if bpy.app.timers.is_registered(_show_requests):
        bpy.app.timers.unregister(_show_requests)
    _state.update(server=None, streamer=None, advertiser=None, addresses=[], last_ui=None, asking=[])


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
    max_texture_size: bpy.props.EnumProperty(
        name="Texture Size",
        items=(
            ("512", "512 px", "Fastest, for older iPhones and big scenes"),
            ("1024", "1024 px", "Sharp enough for most objects"),
            ("2048", "2048 px", "Sharpest, uses more memory on the device"),
        ),
        default="1024",
        description="Image textures are scaled down to this size on their longest side before they're sent",
    )
    auto_start: bpy.props.BoolProperty(
        name="Start Server with Blender",
        default=False,
        description="Start streaming automatically whenever Blender opens",
    )
    require_approval: bpy.props.BoolProperty(
        name="Ask Before Devices Connect",
        default=True,
        description="A device that hasn't connected before gets the scene, and can change it, only once you allow it here",
    )
    trusted_devices: bpy.props.StringProperty(
        name="Allowed Devices",
        default="{}",
        options={"HIDDEN"},
        description="Devices allowed before, so they aren't asked about again",
    )
    study_folder: bpy.props.StringProperty(
        name="Study Folder",
        subtype="DIR_PATH",
        default="",
        description="Where study sessions are saved. Empty uses Documents/ProjectAR Study",
    )
    study_tasks: bpy.props.StringProperty(
        name="Tasks",
        default="",
        description="The tasks or conditions every participant does, separated by commas, as in the app's Study Mode",
    )
    study_counterbalance: bpy.props.BoolProperty(
        name="Counterbalance Order",
        default=True,
        description="Give each participant one order of a balanced Latin square, the same orders the app gives",
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


class ViewARStudy(bpy.types.PropertyGroup):
    """The next study session's participant. Not saved into .blend files."""

    participant: bpy.props.StringProperty(name="Participant ID", description="For example P07. Its number picks the task order")
    condition: bpy.props.StringProperty(name="Condition", description="Optional, for example Screen")
    notes: bpy.props.StringProperty(name="Notes", description="Optional")
    consent: bpy.props.BoolProperty(
        name="Participant Agreed to Take Part",
        description="Confirm consent as your ethics approval requires before starting",
    )
    order: bpy.props.IntProperty(
        name="Order", min=0, default=0,
        description="Which counterbalanced order to use. 0 uses the one the participant ID suggests",
    )


def _study_folder(context=None):
    prefs = _prefs(context)
    folder = bpy.path.abspath(prefs.study_folder) if prefs is not None and prefs.study_folder.strip() else ""
    return folder or study.default_folder()


def _study_plan(context):
    """Task names, whether they're counterbalanced, the number of orders and
    the row this participant gets."""
    prefs = _prefs(context)
    names = study.task_names(prefs.study_tasks if prefs else "")
    counterbalance = bool(prefs and prefs.study_counterbalance)
    count = study.order_count(names, counterbalance)
    settings = context.window_manager.viewar_study
    if settings.order > 0:
        row = (settings.order - 1) % count
    else:
        row = study.suggested_row(settings.participant, len(study.sessions_in(_study_folder(context))), count)
    return names, counterbalance, count, row


def _format_seconds(seconds):
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


@persistent
def _on_study_depsgraph(scene, depsgraph):
    try:
        study.on_depsgraph_update(scene, depsgraph)
    except Exception as exc:  # never let logging break editing
        print(f"ProjectAR: study logging failed: {exc}")


@persistent
def _on_study_undo(*_args):
    study.log("edit.undo")


@persistent
def _on_study_redo(*_args):
    study.log("edit.redo")


@persistent
def _on_study_save(*_args):
    study.log("file.save", {"file": bpy.path.basename(bpy.data.filepath)})


@persistent
def _on_study_load(*_args):
    study.log("file.open", {"file": bpy.path.basename(bpy.data.filepath) or "unsaved"})


_STUDY_HANDLERS = (
    ("depsgraph_update_post", _on_study_depsgraph),
    ("undo_post", _on_study_undo),
    ("redo_post", _on_study_redo),
    ("save_post", _on_study_save),
    ("load_post", _on_study_load),
)
_study_ui = {"resumed": False, "last_second": None}


def _study_tick():
    try:
        if not _study_ui["resumed"]:
            _study_ui["resumed"] = True
            if study.resume(_study_folder()) is not None:
                print("ProjectAR: resumed the study session left running")
        study.tick()
        session = study.active()
        if session is not None and not bpy.app.background:
            second = int(session.elapsed())
            if second != _study_ui["last_second"]:
                _study_ui["last_second"] = second
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == "VIEW_3D":
                            for region in area.regions:
                                if region.type == "UI":
                                    region.tag_redraw()
    except Exception as exc:
        print(f"ProjectAR: study timer: {exc}")
    return 0.5 if study.active() is not None else 1.0


def _draw_settings(layout, prefs):
    col = layout.column()
    sub = col.column()
    sub.enabled = _state["server"] is None
    sub.prop(prefs, "port")
    col.prop(prefs, "update_rate")
    col.prop(prefs, "max_triangles")
    col.prop(prefs, "max_instances")
    col.prop(prefs, "max_texture_size")
    col.prop(prefs, "auto_start")
    col.prop(prefs, "require_approval")
    devices = _trusted_devices(prefs)
    if devices:
        box = col.box()
        box.label(text="Allowed Devices")
        for key, name in sorted(devices.items(), key=lambda item: item[1].lower()):
            row = box.row()
            row.label(text=name, icon="MOD_PHYSICS" if key.startswith("id:") else "URL")
            op = row.operator(VIEWAR_OT_forget_device.bl_idname, text="", icon="X", emboss=False)
            op.key = key
    col.prop(prefs, "study_folder")


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


class VIEWAR_OT_answer_device(bpy.types.Operator):
    bl_idname = "viewar.answer_device"
    bl_label = "Allow or Deny Device"
    bl_description = "Let this device see and change the scene, or turn it away"

    device: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    allow: bpy.props.BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        if properties.allow:
            return "Let this device see and change the scene. It won't be asked about again"
        return "Turn this device away. It can ask again in a minute"

    def execute(self, context):
        streamer = _state["streamer"]
        client = next((c for c in streamer.requests if c.label == self.device), None) if streamer else None
        if client is None:
            self.report({"INFO"}, "That device is no longer waiting")
            return {"CANCELLED"}
        name = client.display_name
        # A click already on its way when the request appeared isn't an answer.
        if self.allow and time.monotonic() - client.pending_since < ANSWER_DELAY:
            self.report({"INFO"}, "Read the request first, then press Allow")
            return {"CANCELLED"}
        key = streamer.answer(self.device, self.allow)
        if key is None:
            return {"CANCELLED"}
        if self.allow:
            prefs = _prefs(context)
            devices = _trusted_devices(prefs)
            devices[key] = name
            _set_trusted_devices(prefs, devices)
            # Keep the choice even if Blender closes before preferences are saved.
            if context.preferences.use_preferences_save and not bpy.app.background:
                bpy.ops.wm.save_userpref()
        self.report({"INFO"}, f"{'Allowed' if self.allow else 'Turned away'} {name}")
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
        return {"FINISHED"}


class VIEWAR_OT_forget_device(bpy.types.Operator):
    bl_idname = "viewar.forget_device"
    bl_label = "Forget Device"
    bl_description = "Ask again the next time this device connects"

    key: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        prefs = _prefs(context)
        devices = _trusted_devices(prefs)
        if devices.pop(self.key, None) is None:
            return {"CANCELLED"}
        _set_trusted_devices(prefs, devices)
        if context.preferences.use_preferences_save and not bpy.app.background:
            bpy.ops.wm.save_userpref()
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
        # Exactly one option showing, so Blender and the app agree: the one
        # already showing if there is just one, else the first.
        layers = review.layer_collections(context.view_layer)
        shown = [c.name for c in coll.children if review._is_shown(layers.get(c.name))]
        review.apply_variant(context.view_layer, coll.name, shown[0] if len(shown) == 1 else coll.children[0].name)
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
        study.log("view.variant", {"set": self.collection, "option": self.option})
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
        study.log("note.add", {"text": self.text.strip()})
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
        study.log("note.remove", {"id": self.note_id})
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


def _open_window(context, area_type):
    """Opens a new Blender window showing one editor, and returns its area."""
    known = {w.as_pointer() for w in context.window_manager.windows}
    try:
        if bpy.ops.wm.window_new() != {"FINISHED"}:
            return None
    except RuntimeError:  # no window to open one from, as in some contexts
        return None
    window = next((w for w in context.window_manager.windows if w.as_pointer() not in known), None)
    if window is None or not window.screen.areas:
        return None
    area = window.screen.areas[0]
    area.type = area_type
    return area


class VIEWAR_OT_show_decision_photo(bpy.types.Operator):
    bl_idname = "viewar.show_decision_photo"
    bl_label = "Show Photo"
    bl_description = "Open the photo taken in the room when this decision was made"

    decision_id: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        decision = review.find_decision(context.scene, self.decision_id)
        image = review.decision_image(decision) if decision else None
        if image is None:
            self.report({"WARNING"}, "This decision has no photo")
            return {"CANCELLED"}
        area = _open_window(context, "IMAGE_EDITOR")
        if area is None:
            self.report({"INFO"}, f"The photo is the image \"{image.name}\"")
            return {"CANCELLED"}
        area.spaces.active.image = image
        return {"FINISHED"}


class VIEWAR_OT_open_decision_log(bpy.types.Operator):
    bl_idname = "viewar.open_decision_log"
    bl_label = "Open Log"
    bl_description = "Read every decision, with its options, criteria and reason, in a Text Editor"

    def execute(self, context):
        text = review.decisions_text(context.scene)
        if text is None:
            return {"CANCELLED"}
        area = _open_window(context, "TEXT_EDITOR")
        if area is None:
            self.report({"INFO"}, f"The log is the text \"{text.name}\"")
            return {"CANCELLED"}
        space = area.spaces.active
        space.text = text
        space.show_word_wrap = True
        return {"FINISHED"}


class VIEWAR_OT_apply_decision(bpy.types.Operator):
    bl_idname = "viewar.apply_decision"
    bl_label = "Show Chosen Option"
    bl_description = "Show the option chosen in this decision, here and in the app"
    bl_options = {"REGISTER", "UNDO"}

    decision_id: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        decision = review.find_decision(context.scene, self.decision_id)
        if decision is None or not decision.get("set"):
            return {"CANCELLED"}
        coll = bpy.data.collections.get(decision["set"])
        if coll is None or decision["chosen"] not in coll.children:
            self.report({"WARNING"}, f"\"{decision['chosen']}\" is no longer in {decision['set']}")
            return {"CANCELLED"}
        if not coll.viewar_variant_set:
            self.report({"WARNING"}, f"{decision['set']} is no longer a variant set")
            return {"CANCELLED"}
        if review.apply_variant(context.view_layer, decision["set"], decision["chosen"]):
            study.log("view.variant", {"set": decision["set"], "option": decision["chosen"], "from": "decision"})
        return {"FINISHED"}


class VIEWAR_OT_remove_decision(bpy.types.Operator):
    bl_idname = "viewar.remove_decision"
    bl_label = "Delete Decision"
    bl_description = "Delete this decision and its photo from the file. The app keeps its own copy"
    bl_options = {"REGISTER", "UNDO"}

    decision_id: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if not review.remove_decision(context.scene, self.decision_id):
            return {"CANCELLED"}
        study.log("decision.remove", {"id": self.decision_id})
        return {"FINISHED"}


CUSTOM_SUBJECT = "__custom__"
# Python must keep dynamic enum items alive while Blender shows them.
_enum_items = {"subjects": [], "options": []}
# Answers from a Record Decision that couldn't be saved, offered again next time.
_decision_draft = {}
_PHOTO_FOLDER = os.path.join(tempfile.gettempdir(), "ProjectAR Decision Photos")
DRAFT_PROPERTIES = ("subject", "question", "options_text", "criteria", "other_criteria", "reason", "confidence")


def _split_list(text):
    """Comma-separated text as a list, without blanks or repeats."""
    return list(dict.fromkeys(part.strip() for part in text.split(",") if part.strip()))


def _enum_id(text):
    """Enum identifiers are limited to 63 bytes. Longer names (Blender 5
    allows 255) are shortened and end with a hash of the whole name, so two
    long names that start the same stay apart."""
    if len(text.encode("utf-8")) <= 63:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    head = text
    while len(head.encode("utf-8")) > 52:
        head = head[:-1]
    return f"{head}~{digest}"


def _subject_items(self, context):
    items = [
        (_enum_id(c.name), c.name, f"Choose between the collections in {c.name}", "OUTLINER_COLLECTION", i)
        for i, c in enumerate(review.variant_set_collections(context.scene))
    ]
    items.append((CUSTOM_SUBJECT, "Something Else", "Type the options yourself", "GREASEPENCIL", len(items)))
    _enum_items["subjects"] = items
    return items


def _subject_set(op, context):
    """The variant set collection picked, found by its possibly shortened id."""
    if op.subject == CUSTOM_SUBJECT:
        return None
    return next((c for c in review.variant_set_collections(context.scene) if _enum_id(c.name) == op.subject), None)


def _option_names(op, context):
    if op.subject == CUSTOM_SUBJECT:
        return _split_list(op.options_text)
    coll = _subject_set(op, context)
    return [c.name for c in coll.children] if coll is not None else []


def _option_items(self, context):
    items = [(_enum_id(name), name, "", i) for i, name in enumerate(_option_names(self, context))]
    if not items:
        items = [("NONE", "No Options", "Add options first", 0)]
    _enum_items["options"] = items
    return items


def _shown_option(context, set_name):
    for item in review.variant_sets(context.scene, context.view_layer):
        if item["name"] == set_name:
            return item["active"]
    return None


def _subject_changed(self, context):
    coll = _subject_set(self, context)
    if coll is None:
        if self.question_is_suggested:
            self.question = ""
        return
    if self.question_is_suggested or not self.question.strip():
        self.question = f"Which option for {coll.name}?"
        self.question_is_suggested = True
    shown = _shown_option(context, coll.name)
    if shown:
        self.chosen = _enum_id(shown)


def _question_edited(self, context):
    coll = _subject_set(self, context)
    if coll is not None and self.question == f"Which option for {coll.name}?":
        return
    self.question_is_suggested = False


def _viewport_area(context):
    if context.area is not None and context.area.type == "VIEW_3D":
        return context.area
    screen = context.window.screen if context.window else None
    return next((a for a in screen.areas if a.type == "VIEW_3D"), None) if screen else None


def capture_viewport(context, max_size=1600):
    """JPEG bytes of the 3D view as it looks now, without the interface, or
    None when there's no 3D view or no GPU (as in background mode)."""
    if bpy.app.background:
        return None
    area = _viewport_area(context)
    region = next((r for r in area.regions if r.type == "WINDOW"), None) if area else None
    if region is None or region.width < 2 or region.height < 2:
        return None
    space = area.spaces.active
    scale = min(1.0, max_size / max(region.width, region.height))
    width, height = max(1, round(region.width * scale)), max(1, round(region.height * scale))

    import gpu
    import numpy as np

    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        offscreen.draw_view3d(
            context.scene,
            context.view_layer,
            space,
            region,
            space.region_3d.view_matrix,
            space.region_3d.window_matrix,
            do_color_management=True,
        )
        buffer = offscreen.texture_color.read()
        buffer.dimensions = width * height * 4
        pixels = np.array(buffer, dtype=np.float32)
    finally:
        offscreen.free()
    if pixels.max(initial=0) > 1.0:  # bytes rather than floats
        pixels /= 255.0
    pixels[3::4] = 1.0

    image = bpy.data.images.new("ProjectAR Viewport Capture", width, height, alpha=False)
    path = os.path.join(_PHOTO_FOLDER, f"{time.time_ns()}.jpg")
    try:
        os.makedirs(_PHOTO_FOLDER, exist_ok=True)
        image.pixels.foreach_set(pixels)
        image.filepath_raw = path
        image.file_format = "JPEG"
        try:
            image.save(quality=85)
        except TypeError:  # older Blender without the quality argument
            image.save()
        with open(path, "rb") as f:
            return f.read()
    finally:
        bpy.data.images.remove(image)
        try:
            os.remove(path)
        except OSError:
            pass


class VIEWAR_OT_record_decision(bpy.types.Operator):
    bl_idname = "viewar.record_decision"
    bl_label = "Record Decision"
    bl_description = (
        "Record a design decision made here at the screen: the options, the one chosen, "
        "what mattered and why, with an image of the 3D view. It's logged like decisions made in the app"
    )
    bl_options = {"REGISTER", "UNDO"}

    subject: bpy.props.EnumProperty(name="Deciding", items=_subject_items, update=_subject_changed, options={"SKIP_SAVE"})
    question: bpy.props.StringProperty(name="Question", update=_question_edited, options={"SKIP_SAVE"})
    question_is_suggested: bpy.props.BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    options_text: bpy.props.StringProperty(
        name="Options", description="The options compared, separated by commas", options={"SKIP_SAVE"}
    )
    chosen: bpy.props.EnumProperty(name="Chosen", items=_option_items, options={"SKIP_SAVE"})
    criteria: bpy.props.BoolVectorProperty(size=len(review.SUGGESTED_CRITERIA), options={"SKIP_SAVE"})
    other_criteria: bpy.props.StringProperty(
        name="Other", description="Anything else that mattered, separated by commas", options={"SKIP_SAVE"}
    )
    reason: bpy.props.StringProperty(name="Reason", description="Why this option", options={"SKIP_SAVE"})
    confidence: bpy.props.EnumProperty(
        name="How Sure",
        items=[("0", "Skip", "Not given")] + [(str(n), str(n), "") for n in range(1, 6)],
        default="0",
        options={"SKIP_SAVE"},
    )
    author: bpy.props.StringProperty(name="By", default="Blender", description="Who made the decision")
    include_photo: bpy.props.BoolProperty(
        name="Include Image of the 3D View", default=True,
        description="Keep an image of the 3D view as it was when you clicked Record Decision",
    )
    photo_path: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        sets = review.variant_set_collections(context.scene)
        draft = _decision_draft.pop(context.scene.name, None)
        if draft:
            self.question_is_suggested = False
            for key in DRAFT_PROPERTIES:
                try:
                    setattr(self, key, draft[key])
                except (TypeError, ValueError):  # a set that's gone
                    pass
            if draft.get("chosen") in _option_names(self, context):
                self.chosen = _enum_id(draft["chosen"])
        elif sets:
            # The set holding the active object, if any, is the likely one.
            active = context.active_object
            owner = next((c for c in sets if active and active.name in c.all_objects), sets[0])
            self.subject = _enum_id(owner.name)
            _subject_changed(self, context)
        else:
            self.subject = CUSTOM_SUBJECT
        if self.include_photo:
            # Taken before the dialog covers the view, as the app does.
            try:
                photo = capture_viewport(context)
            except Exception as exc:  # never block recording over the image
                print(f"ProjectAR: could not capture the 3D view: {exc}")
                photo = None
            if photo:
                os.makedirs(_PHOTO_FOLDER, exist_ok=True)
                self.photo_path = os.path.join(_PHOTO_FOLDER, f"{time.time_ns()}-decision.jpg")
                with open(self.photo_path, "wb") as f:
                    f.write(photo)
        return context.window_manager.invoke_props_dialog(self, width=440, confirm_text="Record")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "subject")
        layout.prop(self, "question")
        if self.subject == CUSTOM_SUBJECT:
            layout.prop(self, "options_text")
        layout.prop(self, "chosen")

        layout.separator()
        heading = layout.column(heading="What Mattered")
        for index, label in enumerate(review.SUGGESTED_CRITERIA):
            heading.prop(self, "criteria", index=index, text=label)
        layout.prop(self, "other_criteria")

        layout.separator()
        layout.prop(self, "reason")
        row = layout.row()
        row.prop(self, "confidence", expand=True)
        layout.prop(self, "author")
        if self.photo_path:
            layout.label(text="An image of the 3D view will be kept with it.", icon="IMAGE_DATA")
        missing = self._missing(context)
        if missing:
            layout.label(text=missing, icon="INFO")

    def _missing(self, context):
        if not self.question.strip():
            return "Add a question."
        options = _option_names(self, context)
        if not options:
            return "Add the options, separated by commas." if self.subject == CUSTOM_SUBJECT else "This set has no options."
        if not self.chosen:
            return "Choose an option."
        if not self.reason.strip():
            return "Add a reason."
        return ""

    def _photo(self):
        if not self.photo_path:
            return None
        try:
            with open(self.photo_path, "rb") as f:
                return f.read()
        except OSError:
            return None
        finally:
            try:
                os.remove(self.photo_path)
            except OSError:
                pass

    def execute(self, context):
        scene = context.scene
        missing = self._missing(context)
        if missing:
            _decision_draft[scene.name] = {key: getattr(self, key) for key in DRAFT_PROPERTIES}
            _decision_draft[scene.name]["chosen"] = self.chosen
            _decision_draft[scene.name]["criteria"] = tuple(self.criteria)
            self.report({"ERROR"}, f"{missing} Your answers are kept for when you try again.")
            self._photo()  # a new image is taken next time
            return {"CANCELLED"}

        options = _option_names(self, context)
        chosen = next((o for o in options if _enum_id(o) == self.chosen), self.chosen)
        coll = _subject_set(self, context)
        criteria = [label for label, on in zip(review.SUGGESTED_CRITERIA, self.criteria) if on]
        criteria += [c for c in _split_list(self.other_criteria) if c not in criteria]
        decision = review.record_decision(
            scene,
            context.view_layer,
            self.question.strip(),
            options,
            chosen,
            self.reason.strip(),
            set_name=coll.name if coll is not None else "",
            criteria=criteria,
            confidence=int(self.confidence) or None,
            author=self.author.strip() or "Blender",
            photo=self._photo(),
        )
        if decision is None:
            self.report({"ERROR"}, "The decision couldn't be recorded")
            return {"CANCELLED"}
        detail = {"question": decision["question"], "chosen": decision["chosen"], "kind": decision["kind"]}
        if decision.get("set"):
            detail["set"] = decision["set"]
        study.log("decision.add", detail)
        self.report({"INFO"}, f"Recorded: {decision['chosen']}")
        return {"FINISHED"}

    def cancel(self, context):
        self._photo()


class VIEWAR_OT_export_decisions(bpy.types.Operator, ExportHelper):
    bl_idname = "viewar.export_decisions"
    bl_label = "Export Decisions"
    bl_description = (
        "Save every decision as CSV, with the same columns as the app's export plus where it was made, "
        "and the photos in a folder beside it"
    )

    filename_ext = ".csv"
    filter_glob: bpy.props.StringProperty(default="*.csv", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return bool(review.decisions(context.scene))

    def invoke(self, context, event):
        if not self.filepath:
            folder = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else os.path.expanduser("~")
            self.filepath = os.path.join(folder, f"{context.scene.name} Decisions.csv")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        try:
            count, photos = review.export_decisions(context.scene, self.filepath)
        except OSError as exc:
            self.report({"ERROR"}, f"Couldn't export: {exc.strerror or exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Exported {count} decisions and {photos} photos")
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

        for client in streamer.requests:
            box = layout.box()
            box.alert = True
            box.label(text=f"{client.display_name} wants to connect", icon="QUESTION")
            box.label(text="It could see and change this scene.")
            row = box.row(align=True)
            op = row.operator(VIEWAR_OT_answer_device.bl_idname, text="Allow", icon="CHECKMARK")
            op.device, op.allow = client.label, True
            op = row.operator(VIEWAR_OT_answer_device.bl_idname, text="Deny", icon="X")
            op.device, op.allow = client.label, False

        clients = [c for c in streamer.clients if c.approval == "allowed"]
        box = layout.box()
        box.label(
            text=f"Devices: {len(clients)}",
            icon="LINKED" if clients else "UNLINKED",
        )
        for client in clients:
            row = box.row()
            row.label(text=client.display_name)
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
            note_id = review.note_id_of(obj)
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


def _wrapped(layout, text, width=38):
    """Labels don't wrap, so long text is split into lines."""
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            layout.label(text=line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        layout.label(text=line)


class VIEWAR_PT_decisions(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_decisions"
    bl_label = "Decisions"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.operator(VIEWAR_OT_record_decision.bl_idname, icon="ADD")
        items = review.decisions(context.scene)
        if not items:
            col = layout.column(align=True)
            col.label(text="Decisions recorded here or in the")
            col.label(text="app appear here, with their reason.")
            return
        for decision in reversed(items):
            box = layout.box()
            row = box.row()
            row.label(text=decision["question"], icon="DECORATE_KEYFRAME")
            if decision.get("photo"):
                op = row.operator(VIEWAR_OT_show_decision_photo.bl_idname, text="", icon="IMAGE_DATA", emboss=False)
                op.decision_id = decision["id"]
            op = row.operator(VIEWAR_OT_remove_decision.bl_idname, text="", icon="X", emboss=False)
            op.decision_id = decision["id"]
            col = box.column(align=True)
            others = [o for o in decision.get("options", []) if o != decision["chosen"]]
            col.label(text=f"Chose {decision['chosen']}", icon="CHECKMARK")
            if others:
                col.label(text=f"Over {', '.join(others)}")
            if decision.get("criteria"):
                col.label(text=f"For {', '.join(decision['criteria'])}")
            if decision.get("reason"):
                _wrapped(col, decision["reason"])
            if decision.get("source") == "blender":
                col.label(text="Made in Blender", icon="BLENDER")
            else:
                col.label(text="Made in the room", icon="HOME")
            if decision.get("set"):
                op = box.operator(VIEWAR_OT_apply_decision.bl_idname, icon="HIDE_OFF")
                op.decision_id = decision["id"]
        row = layout.row(align=True)
        row.operator(VIEWAR_OT_open_decision_log.bl_idname, icon="TEXT")
        row.operator(VIEWAR_OT_export_decisions.bl_idname, text="Export CSV", icon="EXPORT")


class VIEWAR_OT_study_start(bpy.types.Operator):
    bl_idname = "viewar.study_start"
    bl_label = "Start Session"
    bl_description = "Start logging a study session for this participant, with timestamps"

    @classmethod
    def poll(cls, context):
        settings = context.window_manager.viewar_study
        if study.active() is not None:
            return False
        if not settings.participant.strip():
            cls.poll_message_set("Add a participant ID")
            return False
        if not settings.consent:
            cls.poll_message_set("Confirm the participant agreed to take part")
            return False
        return True

    def execute(self, context):
        settings = context.window_manager.viewar_study
        names, counterbalance, _count, row = _study_plan(context)
        try:
            session = study.start(_study_folder(context), settings.participant, settings.condition, settings.notes,
                                  names, counterbalance, row, consent=settings.consent)
        except (OSError, ValueError) as exc:
            self.report({"ERROR"}, f"Couldn't start the session: {exc}")
            return {"CANCELLED"}
        settings.consent = False
        settings.order = 0
        settings.notes = ""
        self.report({"INFO"}, f"Logging to {session.folder}")
        return {"FINISHED"}


class VIEWAR_OT_study_task(bpy.types.Operator):
    bl_idname = "viewar.study_task"
    bl_label = "Start or End Task"
    bl_description = "Start timing the next task, or end the one being timed"

    @classmethod
    def poll(cls, context):
        return study.active() is not None

    def execute(self, context):
        if study.active().running_task is not None:
            timing = study.end_task()
            self.report({"INFO"}, f"{timing['name']}: {_format_seconds(timing['end'] - timing['start'])}")
        else:
            study.start_task()
        return {"FINISHED"}


class VIEWAR_OT_study_marker(bpy.types.Operator):
    bl_idname = "viewar.study_marker"
    bl_label = "Add Marker"
    bl_description = "Mark this moment in the study log"

    label: bpy.props.StringProperty(name="Label", options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return study.active() is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        study.add_marker(self.label)
        return {"FINISHED"}


class VIEWAR_OT_study_end(bpy.types.Operator):
    bl_idname = "viewar.study_end"
    bl_label = "End Session"
    bl_description = "Stop logging, end any task being timed, and write the session's CSV files"

    @classmethod
    def poll(cls, context):
        return study.active() is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            folder = study.end()
        except OSError as exc:
            self.report({"ERROR"}, f"The session ended but its CSV files couldn't be written: {exc}")
            return {"FINISHED"}
        self.report({"INFO"}, f"Saved to {folder}")
        return {"FINISHED"}


class VIEWAR_OT_study_open_folder(bpy.types.Operator):
    bl_idname = "viewar.study_open_folder"
    bl_label = "Open Study Folder"
    bl_description = "Show the study sessions and their CSV files"

    def execute(self, context):
        folder = _study_folder(context)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.report({"ERROR"}, f"Couldn't open {folder}: {exc}")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=folder)
        return {"FINISHED"}


class VIEWAR_PT_study(bpy.types.Panel):
    bl_idname = "VIEWAR_PT_study"
    bl_label = "Study"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ProjectAR"
    bl_parent_id = "VIEWAR_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        session = study.active()
        if session is None:
            _draw_new_study(context, layout)
        else:
            _draw_running_study(layout, session)
        layout.operator(VIEWAR_OT_study_open_folder.bl_idname, icon="FILE_FOLDER")


def _draw_new_study(context, layout):
    settings = context.window_manager.viewar_study
    prefs = _prefs(context)
    col = layout.column()
    col.prop(settings, "participant")
    col.prop(settings, "condition")
    col.prop(settings, "notes")
    col.separator()
    col.prop(prefs, "study_tasks")
    names, counterbalance, count, row = _study_plan(context)
    if len(names) > 1:
        col.prop(prefs, "study_counterbalance")
        if counterbalance:
            col.prop(settings, "order", text="Order (0 = Suggested)")
            col.label(text=f"Order {row + 1} of {count}:")
            _wrapped(col, ", ".join(study.task_order(names, counterbalance, row)))
    col.separator()
    col.prop(settings, "consent")
    layout.operator(VIEWAR_OT_study_start.bl_idname, icon="PLAY")


def _draw_running_study(layout, session):
    header = session.header
    box = layout.box()
    col = box.column(align=True)
    col.label(text=f"Participant {header['participant']}", icon="USER")
    if header.get("condition"):
        col.label(text=f"Condition {header['condition']}")
    col.label(text=f"Elapsed {_format_seconds(session.elapsed())}", icon="TIME")
    col.label(text=f"{header.get('event_count', 0)} events logged")

    order = header.get("task_order") or []
    timings = session.timings
    if order or timings:
        title = f"Tasks, Order {header['order_number']}" if header.get("order_number") else "Tasks"
        box = layout.box()
        box.label(text=title)
        col = box.column(align=True)
        names = list(order) + [t["name"] for t in timings[len(order):]]
        for index, name in enumerate(names):
            timing = timings[index] if index < len(timings) else None
            if timing is None:
                col.label(text=name, icon="RADIOBUT_OFF")
            elif timing.get("end") is None:
                col.label(text=f"{name}   {_format_seconds(time.time() - timing['start'])}", icon="REC")
            else:
                col.label(text=f"{name}   {_format_seconds(timing['end'] - timing['start'])}", icon="CHECKMARK")

    running = session.running_task
    if running is not None:
        layout.operator(VIEWAR_OT_study_task.bl_idname, text=f"End {running['name']}", icon="PAUSE")
    else:
        layout.operator(VIEWAR_OT_study_task.bl_idname, text=f"Start {session.next_task}", icon="PLAY")
    row = layout.row(align=True)
    row.operator(VIEWAR_OT_study_marker.bl_idname, icon="BOOKMARKS")
    row.operator(VIEWAR_OT_study_end.bl_idname, icon="CANCEL")


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
    VIEWAR_OT_answer_device,
    VIEWAR_OT_forget_device,
    VIEWAR_OT_mark_variant_set,
    VIEWAR_OT_unmark_variant_set,
    VIEWAR_OT_set_variant,
    VIEWAR_OT_add_note,
    VIEWAR_OT_remove_note,
    VIEWAR_OT_select_note,
    VIEWAR_OT_show_decision_photo,
    VIEWAR_OT_open_decision_log,
    VIEWAR_OT_apply_decision,
    VIEWAR_OT_remove_decision,
    VIEWAR_OT_record_decision,
    VIEWAR_OT_export_decisions,
    VIEWAR_OT_study_start,
    VIEWAR_OT_study_task,
    VIEWAR_OT_study_marker,
    VIEWAR_OT_study_end,
    VIEWAR_OT_study_open_folder,
    ViewARStudy,
    VIEWAR_PT_main,
    VIEWAR_PT_streaming,
    VIEWAR_PT_variants,
    VIEWAR_PT_notes,
    VIEWAR_PT_decisions,
    VIEWAR_PT_study,
    VIEWAR_PT_stats,
    VIEWAR_PT_settings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.viewar = bpy.props.PointerProperty(type=ViewARSession)
    bpy.types.WindowManager.viewar_study = bpy.props.PointerProperty(type=ViewARStudy)
    for name, handler in _STUDY_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)
    _study_ui.update(resumed=False, last_second=None)
    if not bpy.app.timers.is_registered(_study_tick):
        bpy.app.timers.register(_study_tick, first_interval=1.0, persistent=True)
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
    if bpy.app.timers.is_registered(_study_tick):
        bpy.app.timers.unregister(_study_tick)
    for name, handler in _STUDY_HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
    study.flush_bursts(force=True)
    del bpy.types.WindowManager.viewar_study
    del bpy.types.Collection.viewar_variant_set
    del bpy.types.Object.viewar_visible
    del bpy.types.WindowManager.viewar
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
