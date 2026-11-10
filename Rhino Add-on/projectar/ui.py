# SPDX-License-Identifier: GPL-3.0-or-later
"""The ProjectAR window in Rhino.

A floating Eto window with the Blender panel's sections as tabs: Connect
(server, devices, requests), Stream (what goes to AR, snapshots, statistics),
Review (variants, notes, decisions), Study and Settings. The window refreshes
twice a second from the controller's timer; lists are only rebuilt when what
they show changes.
"""

import os
import time

import Rhino
import Eto.Drawing as drawing
import Eto.Forms as forms

from . import decisions as records, discovery, study
from .settings import LIMITS, TEXTURE_SIZES

SCOPES = (("VISIBLE", "Visible Objects"), ("SELECTED", "Selected Objects"), ("LAYER", "Layer"))
ORIGINS = (("WORLD", "World Origin"), ("POINT", "Picked Point"))
CUSTOM_SUBJECT = "Something Else"


# ---------------------------------------------------------------- building blocks


def _report(window, exc):
    print(f"ProjectAR: {exc}")
    window.say(str(exc))


def _label(text="", bold=False, wrap=True, color=None):
    label = forms.Label()
    label.Text = text
    if wrap:
        label.Wrap = forms.WrapMode.Word
    if bold:
        label.Font = drawing.SystemFonts.Bold()
    if color is not None:
        label.TextColor = color
    return label


def _set_text(control, value):
    value = value or ""
    if control.Text != value:
        control.Text = value


def _stack(items, horizontal=False, spacing=6, padding=0):
    layout = forms.StackLayout()
    layout.Orientation = forms.Orientation.Horizontal if horizontal else forms.Orientation.Vertical
    layout.Spacing = spacing
    if padding:
        layout.Padding = drawing.Padding(padding)
    if horizontal:
        layout.VerticalContentAlignment = forms.VerticalAlignment.Center
    else:
        layout.HorizontalContentAlignment = forms.HorizontalAlignment.Stretch
    for item in items:
        if item is None:
            continue
        control, expand = item if isinstance(item, tuple) else (item, False)
        layout.Items.Add(forms.StackLayoutItem(control, expand))
    return layout


def _row(*items):
    return _stack(items, horizontal=True)


def _group(title, *items):
    box = forms.GroupBox()
    box.Text = title
    box.Padding = drawing.Padding(6)
    box.Content = _stack(items)
    return box


def _dropdown(labels, index, on_change):
    drop = forms.DropDown()
    for text in labels:
        drop.Items.Add(text)
    drop.SelectedIndex = index
    drop.SelectedIndexChanged += lambda sender, e: on_change(drop.SelectedIndex)
    return drop


def _check(text, checked, on_change):
    box = forms.CheckBox()
    box.Text = text
    box.Checked = bool(checked)
    box.CheckedChanged += lambda sender, e: on_change(bool(box.Checked))
    return box


def _stepper(value, low, high, on_change):
    stepper = forms.NumericStepper()
    stepper.MinValue = low
    stepper.MaxValue = high
    stepper.DecimalPlaces = 0
    stepper.Value = value
    stepper.ValueChanged += lambda sender, e: on_change(int(stepper.Value))
    return stepper


def _text_box(value, on_change, placeholder=""):
    box = forms.TextBox()
    box.Text = value or ""
    if placeholder:
        box.PlaceholderText = placeholder
    box.TextChanged += lambda sender, e: on_change(box.Text or "")
    return box


def _format_seconds(seconds):
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


# ---------------------------------------------------------------- window


class Window:
    def __init__(self, controller):
        self.c = controller
        self.host = controller.host
        self._keys = {}
        self._device_sizes = {}
        self.visible = False
        form = forms.Form()
        form.Title = "ProjectAR"
        form.ClientSize = drawing.Size(410, 720)
        form.Resizable = True
        form.Minimizable = False
        form.Maximizable = False
        try:
            # McNeel: MainWindow isn't right on macOS, where each document has its own window.
            for_document = getattr(Rhino.UI.RhinoEtoApp, "MainWindowForDocument", None)
            doc = Rhino.RhinoDoc.ActiveDoc
            form.Owner = for_document(doc) if for_document is not None and doc is not None else Rhino.UI.RhinoEtoApp.MainWindow
        except Exception:
            pass
        self.tabs = forms.TabControl()
        self._pages = {}
        for title, build in (("Connect", self._build_connect), ("Stream", self._build_stream),
                             ("Review", self._build_review), ("Study", self._build_study),
                             ("Settings", self._build_settings)):
            page = forms.TabPage()
            page.Text = title
            scroll = forms.Scrollable()
            scroll.Border = getattr(forms.BorderType, "None")
            scroll.ExpandContentWidth = True
            scroll.Content = build()
            page.Content = scroll
            self.tabs.Pages.Add(page)
            self._pages[title] = page
        form.Content = self.tabs
        form.Closed += self._on_closed
        self.form = form

    def show(self, page=None):
        if not self.visible:
            self.form.Show()
            self.visible = True
        if page in self._pages:
            self.tabs.SelectedPage = self._pages[page]
        self.form.BringToFront()

    def _on_closed(self, sender, e):
        self.visible = False
        if self.c.window is self:
            self.c.window = None  # the next ProjectAR opens a fresh window

    def say(self, message):
        """A short message under the current tab's buttons."""
        for label in (self.connect_message, self.stream_message, self.review_message, self.study_message):
            _set_text(label, message)
        self._said = time.monotonic()

    def _changed(self, name, value):
        if self._keys.get(name) == value:
            return False
        self._keys[name] = value
        return True

    def _do(self, action):
        """Runs a button's action, showing what went wrong instead of failing quietly."""
        def handler():
            try:
                result = action()
                if isinstance(result, str) and result:
                    self.say(result)
            except Exception as exc:
                _report(self, exc)
            self.refresh()
        return handler

    def _button(self, text, action, tip=None):
        button = forms.Button()
        button.Text = text
        run = self._do(action)
        button.Click += lambda sender, e: run()
        if tip:
            button.ToolTip = tip
        return button

    def refresh(self):
        if not self.visible:
            return
        if getattr(self, "_said", None) and time.monotonic() - self._said > 8:
            self._said = None
            self.say("")
        for part in (self._refresh_connect, self._refresh_stream, self._refresh_review,
                     self._refresh_study, self._refresh_settings):
            try:
                part()
            except Exception as exc:
                print(f"ProjectAR: could not update the window: {exc}")

    # ------------------------------------------------------------ Connect

    def _build_connect(self):
        self.error_label = _label(color=drawing.Colors.Red)
        self.status_label = _label(bold=True)
        self.start_button = self._button("Start Server", self.c.start, "Stream this document to the ProjectAR app on your local network")
        self.stop_button = self._button("Stop", self.c.stop, "Stop streaming and disconnect all devices")
        self.copy_button = self._button("Copy Address", self._copy, "With Universal Clipboard you can paste it straight into the app")
        self.resync_button = self._button("Resync", self.c.resync, "Send the whole document to all connected devices again")
        self.address_label = _label()
        self.discovery_label = _label()
        self.requests_panel = forms.Panel()
        self.devices_panel = forms.Panel()
        self.connect_message = _label()
        return _stack([
            self.error_label,
            self.status_label,
            _row(self.start_button, self.stop_button, self.copy_button, self.resync_button),
            self.address_label,
            self.discovery_label,
            self.requests_panel,
            _group("Devices", self.devices_panel),
            self.connect_message,
            _label("Open ProjectAR on the iPhone or iPad, pick this computer under Nearby (or type the "
                   "address), then tap a surface to place the model. Both need the same Wi-Fi."),
        ], padding=10)

    def _copy(self):
        address = self.c.copy_address()
        return f"Copied {address}" if address else None

    def _refresh_connect(self):
        c = self.c
        _set_text(self.error_label, c.error or self.host.error)
        running = c.running
        self.start_button.Visible = not running
        self.stop_button.Visible = running
        self.copy_button.Visible = running
        self.resync_button.Visible = running
        if not running:
            _set_text(self.status_label, "Not streaming")
            _set_text(self.address_label, f"Port {c.settings['port']}")
            _set_text(self.discovery_label, "" if discovery.AVAILABLE else
                      "Auto discovery unavailable. Enter this computer's address in the app.")
        else:
            streamer = c.streamer
            _set_text(self.status_label, f"Streaming {self.host.doc_name()}" + ("  (paused)" if c.session["paused"] else ""))
            port = c.server.port
            lines = [f"{c.addresses[0]}:{port}"] + [f"or {a}:{port}" for a in c.addresses[1:3]]
            _set_text(self.address_label, "\n".join(lines))
            advertiser = c.advertiser
            _set_text(self.discovery_label, "Visible to nearby devices" if advertiser is not None and advertiser.active
                      else "Enter the address in the app")
        streamer = c.streamer
        requests = list(streamer.requests) if streamer else []
        if self._changed("requests", tuple(r.label for r in requests)):
            boxes = []
            for client in requests:
                label = client.label
                boxes.append(_group(
                    f"{client.display_name} wants to connect",
                    _label("It could see and change this document."),
                    _row(self._button("Allow", lambda label=label: self.c.answer(label, True)),
                         self._button("Deny", lambda label=label: self.c.answer(label, False))),
                ))
            self.requests_panel.Content = _stack(boxes)
        clients = [cl for cl in (streamer.clients if streamer else []) if cl.approval == "allowed"]
        if self._changed("devices", tuple((cl.label, cl.display_name) for cl in clients)):
            rows, self._device_sizes = [], {}
            for client in clients:
                size = _label(wrap=False)
                self._device_sizes[client.label] = size
                label = client.label
                rows.append(_row((_label(client.display_name), True), size,
                                 self._button("Disconnect", lambda label=label: self.c.disconnect(label))))
            if not rows:
                rows = [_label("None connected")]
            self.devices_panel.Content = _stack(rows)
        for client in clients:
            size = self._device_sizes.get(client.label)
            if size is not None:
                _set_text(size, f"{client.bytes_sent / 1e6:.1f} MB")

    # ------------------------------------------------------------ Stream

    def _build_stream(self):
        session = self.c.session
        scope_index = next(i for i, (k, _) in enumerate(SCOPES) if k == session["scope"])
        self.scope_drop = _dropdown([t for _, t in SCOPES], scope_index, self._scope_changed)
        self.layer_drop = forms.DropDown()
        self.layer_drop.SelectedIndexChanged += lambda sender, e: self._layer_changed()
        origin_index = next(i for i, (k, _) in enumerate(ORIGINS) if k == session["origin"])
        self.origin_drop = _dropdown([t for _, t in ORIGINS], origin_index, self._origin_changed)
        self.origin_label = _label()
        self.pause_button = self._button("Pause Updates", self._toggle_pause,
                                         "Freeze what devices show while you make heavy edits. Changes are sent when you resume")
        self.snapshot_button = self._button("Send Snapshot", self.c.send_snapshot,
                                            "Send what's streamed as a USDZ file, with materials and textures, to the app's Library")
        self.hidden_label = _label()
        self.stats_labels = [_label() for _ in range(5)]
        self.skipped_label = _label(color=drawing.Colors.OrangeRed)
        self.stream_message = _label()
        return _stack([
            _label("Stream"),
            self.scope_drop,
            self.layer_drop,
            _label("AR origin (lands where you tap in the app)"),
            self.origin_drop,
            _row(self._button("Use Selection", self._origin_from_selection,
                              "The bottom center of the selection becomes the AR origin"),
                 self._button("Pick Point", self._pick_origin),
                 self._button("Clear", lambda: self.host.set_origin(None))),
            self.origin_label,
            _check("Include Blocks", session["include_instances"], lambda v: session.__setitem__("include_instances", v)),
            _check("Stream Lights and Cameras", session["stream_lights"], lambda v: session.__setitem__("stream_lights", v)),
            _row(self.pause_button, self.snapshot_button),
            _group("Show in AR",
                   _row(self._button("Keep Selected Out", lambda: self._hide(True),
                                     "Keeps the selected objects out of AR while they stay visible in Rhino"),
                        self._button("Show Selected", lambda: self._hide(False))),
                   self.hidden_label),
            _group("Statistics", *self.stats_labels, self.skipped_label),
            self.stream_message,
        ], padding=10)

    def _scope_changed(self, index):
        self.c.session["scope"] = SCOPES[max(index, 0)][0]
        self.refresh()

    def _layer_changed(self):
        paths = self._keys.get("layers") or ()
        index = self.layer_drop.SelectedIndex
        if 0 <= index < len(paths):
            self.c.session["layer"] = paths[index]

    def _origin_changed(self, index):
        self.c.session["origin"] = ORIGINS[max(index, 0)][0]

    def _toggle_pause(self):
        self.c.session["paused"] = not self.c.session["paused"]

    def _origin_from_selection(self):
        with self.host.undoable("ProjectAR Origin"):
            if not self.host.set_origin_from_selection():
                return "Select the objects to stand on the floor first."
        self.c.session["origin"] = "POINT"
        self.origin_drop.SelectedIndex = 1
        return None

    def _pick_origin(self):
        location = self.host.pick_point("Pick the point that lands where you tap in the app")
        if location is None:
            return "No point picked. Try Use Selection instead."
        with self.host.undoable("ProjectAR Origin"):
            self.host.set_origin(location)
        self.c.session["origin"] = "POINT"
        self.origin_drop.SelectedIndex = 1
        return None

    def _hide(self, hidden):
        with self.host.undoable("ProjectAR Show in AR"):
            count = self.host.set_ar_hidden(hidden)
        if not count:
            return "Select objects first." if not self.host.selected_labels() else "Nothing to change."
        return f"{'Kept' if hidden else 'Put back'} {count} object{'s' if count != 1 else ''} {'out of' if hidden else 'in'} AR"

    def _refresh_stream(self):
        c, session = self.c, self.c.session
        paths = tuple(self.host.layer_paths())
        if self._changed("layers", paths):
            self.layer_drop.Items.Clear()
            for path in paths:
                self.layer_drop.Items.Add(path)
            if session["layer"] in paths:
                self.layer_drop.SelectedIndex = paths.index(session["layer"])
            elif paths:
                session["layer"] = paths[0]
                self.layer_drop.SelectedIndex = 0
        self.layer_drop.Visible = session["scope"] == "LAYER"
        origin = self.host.origin_point()
        if session["origin"] == "POINT":
            _set_text(self.origin_label, "Origin at " + ", ".join(f"{v:.3g}" for v in origin) if origin else
                      "No point picked yet: the world origin is used.")
        else:
            _set_text(self.origin_label, "")
        _set_text(self.pause_button, "Resume Updates" if session["paused"] else "Pause Updates")
        self.pause_button.Enabled = c.running
        streamer = c.streamer
        self.snapshot_button.Enabled = bool(streamer and streamer.snapshot_clients and not streamer.snapshot_busy)
        hidden = self.host.hidden_count()
        _set_text(self.hidden_label, f"{hidden} object{'s' if hidden != 1 else ''} kept out of AR" if hidden else "")
        if streamer is None or not streamer.clients:
            lines = ["Connect a device to see statistics.", "", "", "", ""]
            skipped = ""
        else:
            lines = [
                f"Objects: {streamer.object_count}",
                f"Block instances: {streamer.instance_count}"
                + ("  (limit reached)" if streamer.instances_capped else ""),
                f"Unique block meshes: {streamer.prototype_count}" if streamer.instance_count else "",
                f"Triangles: {streamer.triangle_count:,}",
                "Heavy for older iPhones" if c.heavy() else "",
            ]
            names = streamer.skipped
            skipped = ""
            if names:
                skipped = "Over the triangle limit, not sent: " + ", ".join(names[:5])
                if len(names) > 5:
                    skipped += f" and {len(names) - 5} more"
        for label, line in zip(self.stats_labels, lines):
            _set_text(label, line)
        _set_text(self.skipped_label, skipped)

    # ------------------------------------------------------------ Review

    def _build_review(self):
        self.variants_panel = forms.Panel()
        self.notes_panel = forms.Panel()
        self.decisions_panel = forms.Panel()
        self.note_text = forms.TextBox()
        self.note_text.PlaceholderText = "Note text"
        self.review_message = _label()
        return _stack([
            _group("Variants",
                   _label("Put each design option on its own sublayer of one parent layer, make the "
                          "parent current, then:"),
                   self._button("Use Current Layer as Variant Set", self._mark_set),
                   self.variants_panel),
            _group("Notes",
                   _label("Notes left in the app appear here, and as text dots on the ProjectAR Notes layer."),
                   self.note_text,
                   self._button("Add Note at Selection", self._add_note,
                                "Leave a note above the selected object. It shows up as a pin in the app"),
                   self.notes_panel),
            _group("Decisions",
                   _row(self._button("Record Decision", self._record_decision,
                                     "Record a decision made here at the screen, with an image of the view"),
                        self._button("Open Log", self._open_log),
                        self._button("Export CSV", self._export_decisions)),
                   self.decisions_panel),
            self.review_message,
        ], padding=10)

    def _mark_set(self):
        with self.host.undoable("ProjectAR Variant Set"):
            return self.host.mark_variant_set()

    def _add_note(self):
        note = (self.note_text.Text or "").strip()
        if not note:
            return "Type the note first."
        with self.host.undoable("ProjectAR Note"):
            added = self.host.add_note_at_selection(note)
        if added:
            self.note_text.Text = ""
            study.log("note.add", {"text": note})
        return None if added else "The note couldn't be added."

    def _set_variant(self, set_name, option):
        with self.host.undoable("ProjectAR Variant"):
            if self.host.apply_variant(set_name, option):
                study.log("view.variant", {"set": set_name, "option": option})

    def _remove_note(self, note_id):
        with self.host.undoable("ProjectAR Delete Note"):
            if self.host.remove_note(note_id):
                study.log("note.remove", {"id": note_id})

    def _refresh_review(self):
        sets = self.host.variant_sets()
        if self._changed("variants", repr(sets)):
            parts = []
            for variant_set in sets:
                name = variant_set["name"]
                buttons = []
                for option in variant_set["options"]:
                    option_name = option["name"]
                    title = f"● {option_name}" if option_name == variant_set["active"] else option_name
                    buttons.append(self._button(title, lambda n=name, o=option_name: self._set_variant(n, o),
                                                "Show this option and hide the others, here and in the app"))
                parts.append(_row((_label(name, bold=True), True),
                                  self._button("Stop Using", lambda n=name: self.host.unmark_variant_set(n),
                                               "Stream this layer as ordinary objects again")))
                parts.append(_stack(buttons))
            self.variants_panel.Content = _stack(parts)
        notes = self.host.notes()
        if self._changed("notes", repr(notes)):
            rows = []
            for note in notes:
                note_id = note["id"]
                rows.append(_label(note["text"] or "(empty)"))
                rows.append(_row((_label(note["author"] or "", wrap=False), True),
                                 self._button("Select", lambda i=note_id: self.host.select_note(i)),
                                 self._button("Delete", lambda i=note_id: self._remove_note(i))))
            self.notes_panel.Content = _stack(rows)
        items = self.host.decisions()
        if self._changed("decisions", repr(items)):
            rows = []
            for d in items:
                decision_id = d["id"]
                lines = [f"Chose {d['chosen']}"]
                others = [o for o in d.get("options", []) if o != d["chosen"]]
                if others:
                    lines.append(f"Over {', '.join(others)}")
                if d.get("criteria"):
                    lines.append(f"For {', '.join(d['criteria'])}")
                if d.get("reason"):
                    lines.append(f"Because {d['reason']}")
                source = d.get("source")
                lines.append("Made in Rhino" if source == "rhino" else "Made in Blender" if source == "blender" else "Made in the room")
                buttons = [self._button("Delete", lambda i=decision_id: self._remove_decision(i))]
                if d.get("photo"):
                    buttons.insert(0, self._button("Photo", lambda i=decision_id, q=d["question"]: self._show_photo(i, q)))
                if d.get("set"):
                    buttons.insert(0, self._button("Show Chosen", lambda i=decision_id: self._apply_decision(i)))
                rows.append(_group(d["question"], _label("\n".join(lines)), _row(*buttons)))
            if not rows:
                rows = [_label("Decisions recorded here or in the app appear here, with their reason.")]
            self.decisions_panel.Content = _stack(rows)

    def _apply_decision(self, decision_id):
        with self.host.undoable("ProjectAR Variant"):
            return self.host.apply_decision(decision_id)

    def _remove_decision(self, decision_id):
        answer = forms.MessageBox.Show(
            self.form, "Delete this decision and its photo from the document? The app keeps its own copy.",
            "ProjectAR", forms.MessageBoxButtons.YesNo, forms.MessageBoxType.Question)
        if answer != forms.DialogResult.Yes:
            return None
        with self.host.undoable("ProjectAR Delete Decision"):
            if self.host.remove_decision(decision_id):
                study.log("decision.remove", {"id": decision_id})
        return None

    def _show_photo(self, decision_id, question):
        path = self.host.decision_photo_file(decision_id)
        if path is None:
            return "This decision has no photo."
        image = drawing.Bitmap(path)
        view = forms.ImageView()
        view.Image = image
        dialog = forms.Dialog()
        dialog.Title = question
        dialog.Resizable = True
        dialog.ClientSize = drawing.Size(min(int(image.Width), 1000), min(int(image.Height), 750))
        dialog.Content = view
        dialog.ShowModal(self.form)
        return None

    def _open_log(self):
        area = forms.TextArea()
        area.ReadOnly = True
        area.Wrap = True
        area.Text = self.host.decisions_log()
        dialog = forms.Dialog()
        dialog.Title = "ProjectAR Decisions"
        dialog.Resizable = True
        dialog.ClientSize = drawing.Size(560, 640)
        dialog.Padding = drawing.Padding(8)
        dialog.Content = area
        dialog.ShowModal(self.form)

    def _export_decisions(self):
        if not self.host.decisions():
            return "There are no decisions to export yet."
        dialog = forms.SaveFileDialog()
        dialog.Title = "Export Decisions"
        dialog.Filters.Add(forms.FileFilter("CSV", ".csv"))
        folder = os.path.dirname(self.host.doc_path()) or os.path.expanduser("~")
        dialog.FileName = os.path.join(folder, f"{self.host.doc_name()} Decisions.csv")
        if dialog.ShowDialog(self.form) != forms.DialogResult.Ok:
            return None
        path = dialog.FileName
        if not path.lower().endswith(".csv"):
            path += ".csv"
        count, photos = self.host.export_decisions(path)
        return f"Exported {count} decisions and {photos} photos"

    def _record_decision(self):
        photo = None
        try:
            photo = self.host.capture_view()  # before the dialog covers the view, as the app does
        except Exception as exc:
            print(f"ProjectAR: could not capture the view: {exc}")
        DecisionDialog(self, photo).show()

    # ------------------------------------------------------------ Study

    def _build_study(self):
        s, form = self.c.settings, self.c.study_form

        def setting(name):
            return lambda value: (s.__setitem__(name, value), self.c.save_settings())

        self.study_setup = _stack([
            _label("For the screen condition: the participant works in Rhino. Tasks and orders match the app's Study Mode."),
            _label("Tasks, separated by commas"),
            _text_box(s.get("study_tasks", ""), setting("study_tasks"), "Oak, Walnut, Glass"),
            _check("Counterbalance Order", s.get("study_counterbalance", True), setting("study_counterbalance")),
            _label("Participant ID (its number picks the order)"),
            _text_box(form["participant"], lambda v: form.__setitem__("participant", v), "P07"),
            _label("Condition (optional)"),
            _text_box(form["condition"], lambda v: form.__setitem__("condition", v), "Screen"),
            _label("Notes (optional)"),
            _text_box(form["notes"], lambda v: form.__setitem__("notes", v)),
            _row(_label("Order (0 = suggested)", wrap=False),
                 _stepper(int(form["order"]), 0, 999, lambda v: form.__setitem__("order", v))),
        ])
        self.study_order_label = _label()
        self.consent_check = _check("Participant Agreed to Take Part", form["consent"], lambda v: form.__setitem__("consent", v))
        self.study_start_button = self._button("Start Session", self.c.start_session)
        self.study_status = _label()
        self.study_tasks_label = _label()
        self.task_button = self._button("Start Task", self.c.toggle_task)
        self.marker_text = forms.TextBox()
        self.marker_text.PlaceholderText = "Marker label"
        self.study_running = _stack([
            self.study_status,
            self.study_tasks_label,
            _row(self.task_button, self._button("End Session", self.c.end_session)),
            _row((self.marker_text, True), self._button("Add Marker", self._add_marker)),
        ])
        self.study_message = _label()
        return _stack([
            self.study_setup,
            self.study_order_label,
            self.consent_check,
            self.study_start_button,
            self.study_running,
            self.study_message,
            self._button("Open Study Folder", lambda: self._open_study_folder()),
        ], padding=10)

    def _add_marker(self):
        study.add_marker(self.marker_text.Text or "")
        self.marker_text.Text = ""
        return "Marker added"

    def _open_study_folder(self):
        from .app import open_folder

        folder = self.c.study_folder()
        os.makedirs(folder, exist_ok=True)
        open_folder(folder)

    def _refresh_study(self):
        session = study.active()
        running = session is not None
        self.study_setup.Visible = not running
        self.consent_check.Visible = not running
        self.study_start_button.Visible = not running
        self.study_running.Visible = running
        if not running:
            if not self.c.study_form["consent"] and self.consent_check.Checked:
                self.consent_check.Checked = False
            names, counterbalance, count, row = self.c.study_plan()
            if len(names) > 1 and counterbalance:
                order = " > ".join(study.task_order(names, counterbalance, row))
                _set_text(self.study_order_label, f"Order {row + 1} of {count}: {order}")
            elif names:
                _set_text(self.study_order_label, "Order: " + " > ".join(names))
            else:
                _set_text(self.study_order_label, "")
            self.study_start_button.Enabled = bool(self.c.study_form["participant"].strip())
            return
        _set_text(self.study_order_label, "")
        header = session.header
        status = [f"Participant {header['participant']}"]
        if header.get("condition"):
            status.append(f"Condition {header['condition']}")
        status.append(f"Elapsed {_format_seconds(session.elapsed())}")
        status.append(f"{header.get('event_count', 0)} events logged")
        _set_text(self.study_status, "\n".join(status))
        lines = []
        timings = session.timings
        order = header.get("task_order") or []
        for i, name in enumerate(order or [t["name"] for t in timings]):
            timing = timings[i] if i < len(timings) else None
            if timing is None:
                lines.append(f"○ {name}")
            elif timing.get("end") is None:
                lines.append(f"● {name}   {_format_seconds(time.time() - timing['start'])}")
            else:
                lines.append(f"✓ {name}   {_format_seconds(timing['end'] - timing['start'])}")
        _set_text(self.study_tasks_label, "\n".join(lines))
        running_task = session.running_task
        _set_text(self.task_button, f"End {running_task['name']}" if running_task else f"Start {session.next_task}")

    # ------------------------------------------------------------ Settings

    def _build_settings(self):
        s = self.c.settings

        def setting(name):
            def update(value):
                s[name] = value
                self.c.save_settings()
            return update

        sizes = list(TEXTURE_SIZES)
        self.port_stepper = _stepper(int(s["port"]), *LIMITS["port"], setting("port"))
        self.devices_panel_settings = forms.Panel()
        self.folder_box = _text_box(s.get("study_folder", ""), setting("study_folder"), study.default_folder())
        return _stack([
            _row(_label("Port", wrap=False), self.port_stepper),
            _row(_label("Updates per second", wrap=False), _stepper(int(s["update_rate"]), *LIMITS["update_rate"], setting("update_rate"))),
            _row(_label("Max triangles per object", wrap=False),
                 _stepper(int(s["max_triangles"]), *LIMITS["max_triangles"], setting("max_triangles"))),
            _row(_label("Max block instances", wrap=False),
                 _stepper(int(s["max_instances"]), *LIMITS["max_instances"], setting("max_instances"))),
            _row(_label("Texture size", wrap=False),
                 _dropdown([f"{n} px" for n in sizes], sizes.index(int(s["max_texture_size"])),
                           lambda i: setting("max_texture_size")(sizes[max(i, 0)]))),
            _check("Start Server When ProjectAR Opens", s["auto_start"], setting("auto_start")),
            _check("Ask Before Devices Connect", s["require_approval"], setting("require_approval")),
            _group("Allowed Devices", self.devices_panel_settings),
            _label("Study folder"),
            _row((self.folder_box, True), self._button("Choose", self._choose_folder)),
            _label("Changes to the port apply the next time the server starts. Ask Before Devices Connect "
                   "should only be off on a network you trust completely."),
        ], padding=10)

    def _choose_folder(self):
        dialog = forms.SelectFolderDialog()
        dialog.Title = "Study Folder"
        if dialog.ShowDialog(self.form) == forms.DialogResult.Ok:
            self.folder_box.Text = dialog.Directory

    def _refresh_settings(self):
        self.port_stepper.Enabled = not self.c.running
        devices = dict(self.c.settings["trusted_devices"])
        if self._changed("trusted", repr(sorted(devices.items()))):
            rows = []
            for device, name in sorted(devices.items(), key=lambda item: item[1].lower()):
                rows.append(_row((_label(name), True),
                                 self._button("Forget", lambda d=device: self.c.forget_device(d),
                                              "Ask again the next time this device connects")))
            self.devices_panel_settings.Content = _stack(rows or [_label("None yet")])


# ---------------------------------------------------------------- Record Decision


class DecisionDialog:
    """Records a decision made at the screen: a variant set (its options
    listed, the shown one picked) or your own options, the option chosen,
    what mattered, the reason, and how sure you are."""

    def __init__(self, window, photo):
        self.window = window
        self.host = window.host
        self.photo = photo
        self.sets = self.host.variant_sets()
        dialog = forms.Dialog()
        dialog.Title = "Record Decision"
        dialog.Padding = drawing.Padding(12)
        dialog.Resizable = True
        dialog.ClientSize = drawing.Size(460, 680)
        self.dialog = dialog

        subjects = [s["name"] for s in self.sets] + [CUSTOM_SUBJECT]
        selected = set(self.host.selected_labels())
        start = next((i for i, s in enumerate(self.sets)
                      if any(selected & set(o["objects"]) for o in s["options"])), 0 if self.sets else len(subjects) - 1)
        self.subject = _dropdown(subjects, start, lambda i: self._subject_changed())
        self.question = forms.TextBox()
        self.options_text = forms.TextBox()
        self.options_text.PlaceholderText = "The options compared, separated by commas"
        self.options_text.TextChanged += lambda sender, e: self._fill_options()
        self.options_label = _label("Options")
        self.chosen = forms.DropDown()
        self.criteria = [_check(c, False, lambda v: None) for c in records.SUGGESTED_CRITERIA]
        self.other = forms.TextBox()
        self.other.PlaceholderText = "Anything else that mattered, separated by commas"
        self.reason = forms.TextArea()
        self.reason.Size = drawing.Size(-1, 70)
        self.confidence = _dropdown(["Skip", "1", "2", "3", "4", "5"], 0, lambda i: None)
        self.author = forms.TextBox()
        self.author.Text = "Rhino"
        self.include_photo = _check("Include Image of the View", photo is not None, lambda v: None)
        self.include_photo.Enabled = photo is not None
        self.missing = _label(color=drawing.Colors.OrangeRed)
        record = forms.Button()
        record.Text = "Record"
        record.Click += lambda sender, e: self._record()
        cancel = forms.Button()
        cancel.Text = "Cancel"
        cancel.Click += lambda sender, e: dialog.Close()
        dialog.DefaultButton = record
        dialog.AbortButton = cancel
        dialog.Content = _stack([
            _label("Deciding"), self.subject,
            _label("Question"), self.question,
            self.options_label, self.options_text,
            _label("Chosen"), self.chosen,
            _label("What mattered"), *self.criteria, self.other,
            _label("Reason"), self.reason,
            _row(_label("How sure", wrap=False), self.confidence),
            _row(_label("By", wrap=False), (self.author, True)),
            self.include_photo,
            self.missing,
            _row(record, cancel),
        ])
        self._suggested = ""
        self._subject_changed()

    def show(self):
        self.dialog.ShowModal(self.window.form)

    def _set(self):
        index = self.subject.SelectedIndex
        return self.sets[index] if 0 <= index < len(self.sets) else None

    def _option_names(self):
        variant_set = self._set()
        if variant_set is not None:
            return [o["name"] for o in variant_set["options"]]
        return list(dict.fromkeys(p.strip() for p in (self.options_text.Text or "").split(",") if p.strip()))

    def _subject_changed(self):
        variant_set = self._set()
        custom = variant_set is None
        self.options_text.Visible = custom
        self.options_label.Visible = custom
        current = self.question.Text or ""
        if not current.strip() or current == self._suggested:
            self._suggested = f"Which option for {variant_set['name']}?" if variant_set else ""
            self.question.Text = self._suggested
        self._fill_options(variant_set["active"] if variant_set else None)

    def _fill_options(self, pick=None):
        previous = self.chosen.SelectedIndex
        names = self._option_names()
        old = self._options if hasattr(self, "_options") else []
        keep = old[previous] if 0 <= previous < len(old) else None
        self._options = names
        self.chosen.Items.Clear()
        for name in names:
            self.chosen.Items.Add(name)
        wanted = pick if pick in names else keep if keep in names else None
        if wanted is not None:
            self.chosen.SelectedIndex = names.index(wanted)
        elif names:
            self.chosen.SelectedIndex = 0

    def _record(self):
        question = (self.question.Text or "").strip()
        options = self._option_names()
        index = self.chosen.SelectedIndex
        chosen = options[index] if 0 <= index < len(options) else ""
        reason = (self.reason.Text or "").strip()
        if not question:
            missing = "Add a question."
        elif not options:
            missing = "Add the options, separated by commas." if self._set() is None else "This set has no options."
        elif not chosen:
            missing = "Choose an option."
        elif not reason:
            missing = "Add a reason."
        else:
            missing = ""
        if missing:
            _set_text(self.missing, missing)
            return
        criteria = [c.Text for c in self.criteria if c.Checked]
        criteria += [c for c in dict.fromkeys(p.strip() for p in (self.other.Text or "").split(",") if p.strip())
                     if c not in criteria]
        variant_set = self._set()
        confidence = self.confidence.SelectedIndex
        with self.host.undoable("ProjectAR Decision"):
            decision = self.host.record_decision(
                question, options, chosen, reason,
                set_name=variant_set["name"] if variant_set else "",
                criteria=criteria,
                confidence=confidence if confidence > 0 else None,
                author=(self.author.Text or "").strip() or "Rhino",
                photo=self.photo if self.include_photo.Checked else None,
            )
        if decision is None:
            _set_text(self.missing, "The decision couldn't be recorded.")
            return
        detail = {"question": decision["question"], "chosen": decision["chosen"], "kind": decision["kind"]}
        if decision.get("set"):
            detail["set"] = decision["set"]
        study.log("decision.add", detail)
        self.window.say(f"Recorded: {decision['chosen']}")
        self.dialog.Close()
