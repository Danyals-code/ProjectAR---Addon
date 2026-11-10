# SPDX-License-Identifier: GPL-3.0-or-later
"""The ProjectAR controller in Rhino: starts and stops the server, runs the
streamer on a UI timer, and keeps study sessions and the window up to date.

There is one controller per Rhino session, kept in scriptcontext.sticky so
running ProjectAR.py again shows the same window instead of a second server.
"""

import os
import subprocess
import sys
import time
import traceback

import Rhino

from . import VERSION, discovery, server, settings as saved, streaming, study
from .rhino_host import RhinoHost

# Seconds a request must have been showing before Allow counts.
ANSWER_DELAY = 1.0
HEAVY_SCENE_TRIANGLES = 1_000_000
IDLE_INTERVAL = 0.25
UI_INTERVAL = 0.5
STICKY_KEY = "projectar.controller"


def open_folder(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 (opening a folder the user chose)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
    except OSError as exc:
        print(f"ProjectAR: could not open {path}: {exc}")


class Controller:
    def __init__(self):
        self.settings = saved.load()
        # This Rhino session's choices, not saved.
        self.session = {
            "scope": "VISIBLE",
            "layer": "",
            "origin": "WORLD",
            "include_instances": True,
            "stream_lights": True,
            "paused": False,
        }
        self.study_form = {"participant": "", "condition": "", "notes": "", "consent": False, "order": 0}
        self.host = RhinoHost()
        self.server = None
        self.streamer = None
        self.advertiser = None
        self.addresses = []
        self.error = ""
        self.message = ""
        self.window = None
        self._timer = None
        self._last_ui = 0.0
        self._last_study = 0.0
        self._resumed = False
        self._closing = None
        self._start_timer()
        try:
            self._closing = self._on_rhino_closing
            Rhino.RhinoApp.Closing += self._closing
        except Exception as exc:
            print(f"ProjectAR: won't stop by itself when Rhino closes: {exc}")
        print(f"ProjectAR for Rhino {VERSION} loaded")

    # ------------------------------------------------------------ timer

    def _start_timer(self):
        import Eto.Forms as forms

        self._timer = forms.UITimer()
        self._timer.Interval = IDLE_INTERVAL
        self._timer.Elapsed += self._tick
        self._timer.Start()

    def _set_interval(self, seconds):
        if self._timer is not None:
            self._timer.Interval = seconds

    def _tick(self, sender, e):
        now = time.monotonic()
        if self.streamer is not None:
            try:
                self.streamer.tick()
                self.error = ""  # a one-off error shouldn't stay up once things work again
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
        if now - self._last_study >= 0.5:
            self._last_study = now
            try:
                if not self._resumed:
                    self._resumed = True
                    if study.resume(self.study_folder(), self.host.doc_file_name()) is not None:
                        print("ProjectAR: resumed the study session left running")
                study.tick(self.host.view_state() if study.active() is not None else None)
            except Exception as exc:
                print(f"ProjectAR: study timer: {exc}")
        if self.window is not None and self.window.visible and now - self._last_ui >= UI_INTERVAL:
            self._last_ui = now
            try:
                if self.streamer is None or not self.streamer.clients:
                    # Keeps the window's variants, notes and decisions current.
                    self.host.scene(self.options())
                self.window.refresh()
            except Exception as exc:
                print(f"ProjectAR: window: {exc}")
                traceback.print_exc()

    def _on_rhino_closing(self, sender, e):
        self.stop()
        try:
            self.host.close()
        except Exception:
            pass
        if self._timer is not None:
            self._timer.Stop()
        saved.save(self.settings)

    # ------------------------------------------------------------ server

    @property
    def running(self):
        return self.server is not None

    def options(self):
        s, session = self.settings, self.session
        return streaming.Options(
            max_triangles=int(s["max_triangles"]),
            include_instances=bool(session["include_instances"]),
            max_instances=int(s["max_instances"]),
            scope=session["scope"],
            layer=session["layer"],
            origin=session["origin"],
            paused=bool(session["paused"]),
            max_texture_size=int(s["max_texture_size"]),
            require_approval=bool(s["require_approval"]),
            trusted=frozenset(s["trusted_devices"]),
            stream_lights=bool(session["stream_lights"]),
        )

    def start(self):
        """Opens the port and starts streaming. Returns an error to show, or None."""
        if self.server is not None:
            return None
        port = int(self.settings["port"])
        srv = server.Server(port)
        try:
            srv.start()  # raises OSError if the port is taken
        except OSError as exc:
            self.error = f"Could not open port {port}: {exc.strerror or exc}. Is Blender or another Rhino streaming on it?"
            return self.error
        self.server = srv
        self.error = ""
        self.addresses = discovery.local_addresses()
        self.streamer = streaming.Streamer(srv, self.host, self.options)
        self.streamer.on_request = self._on_request
        self.advertiser = discovery.Advertiser()
        self.advertiser.start(port, self.addresses)
        self._set_interval(1.0 / max(int(self.settings["update_rate"]), 1))
        Rhino.RhinoApp.WriteLine(f"ProjectAR: streaming on {self.addresses[0]}:{port}")
        return None

    def stop(self):
        if self.advertiser is not None:
            self.advertiser.stop()
        if self.server is not None:
            self.server.stop()
        self.server = self.streamer = self.advertiser = None
        self.addresses = []
        self.session["paused"] = False
        self._set_interval(IDLE_INTERVAL)

    def resync(self):
        if self.streamer is not None:
            self.streamer.request_full_sync()

    def send_snapshot(self):
        if self.streamer is None or not self.streamer.snapshot_clients:
            return "Connect a device that can save snapshots first."
        if self.streamer.snapshot_busy:
            return "A snapshot is on its way."
        count = len(self.streamer.snapshot_clients)
        self.streamer.request_snapshot()
        return f"Sending a snapshot to {count} device{'s' if count != 1 else ''}"

    def copy_address(self):
        if self.server is None or not self.addresses:
            return None
        import Eto.Forms as forms

        address = f"{self.addresses[0]}:{self.server.port}"
        forms.Clipboard().Text = address
        return address

    def disconnect(self, label):
        if self.server is not None:
            self.server.disconnect(label)

    def _on_request(self, client):
        """A device asks to connect: say so where it can't be missed. Only
        text: Allow is in the window, read before it's pressed."""
        Rhino.RhinoApp.WriteLine(
            f"ProjectAR: {client.display_name} wants to see and change this document. "
            "Allow or deny it in the ProjectAR window."
        )
        try:
            self.show(page="Connect")
        except Exception as exc:
            print(f"ProjectAR: could not show the request: {exc}")

    def answer(self, label, allow):
        """Allows or turns away a waiting device. Returns a message to show."""
        streamer = self.streamer
        client = next((c for c in streamer.requests if c.label == label), None) if streamer else None
        if client is None:
            return "That device is no longer waiting"
        name = client.display_name
        # A click already on its way when the request appeared isn't an answer.
        if allow and time.monotonic() - client.pending_since < ANSWER_DELAY:
            return "Read the request first, then press Allow"
        device = streamer.answer(label, allow)
        if device is None:
            return "That device is no longer waiting"
        if allow:
            self.settings["trusted_devices"][device] = name
            saved.save(self.settings)
        return f"{'Allowed' if allow else 'Turned away'} {name}"

    def forget_device(self, device):
        if self.settings["trusted_devices"].pop(device, None) is not None:
            saved.save(self.settings)

    def save_settings(self):
        saved.save(self.settings)

    # ------------------------------------------------------------ study

    def study_folder(self):
        folder = self.settings.get("study_folder", "").strip()
        return os.path.expanduser(folder) if folder else study.default_folder()

    def study_plan(self):
        """Task names, whether they're counterbalanced, the number of orders
        and the row this participant gets."""
        names = study.task_names(self.settings.get("study_tasks", ""))
        counterbalance = bool(self.settings.get("study_counterbalance", True))
        count = study.order_count(names, counterbalance)
        order = int(self.study_form.get("order") or 0)
        if order > 0:
            row = (order - 1) % count
        else:
            row = study.suggested_row(self.study_form.get("participant", ""), len(study.sessions_in(self.study_folder())), count)
        return names, counterbalance, count, row

    def start_session(self):
        form = self.study_form
        if not form.get("consent"):
            return "Confirm the participant agreed to take part first."
        names, counterbalance, _, row = self.study_plan()
        try:
            study.start(self.study_folder(), form["participant"], form["condition"], form["notes"],
                        names, counterbalance, row, consent=True, host=self.host.study_host())
        except (OSError, ValueError) as exc:
            return f"Could not start the session: {exc}"
        form["consent"] = False  # each participant confirms again
        return None

    def toggle_task(self):
        session = study.active()
        if session is None:
            return
        if session.running_task is not None:
            study.end_task()
        else:
            study.start_task()

    def end_session(self):
        try:
            folder = study.end()
        except OSError as exc:
            return f"Could not write the session files: {exc}"
        return f"Saved to {folder}" if folder else None

    # ------------------------------------------------------------ window

    def show(self, page=None):
        from .ui import Window

        if self.window is None:
            self.window = Window(self)
            if self.settings.get("auto_start") and not self.running:
                self.start()
        self.window.show(page)
        self.window.refresh()

    def heavy(self):
        return self.streamer is not None and self.streamer.triangle_count > HEAVY_SCENE_TRIANGLES


def controller():
    try:
        import scriptcontext

        sticky = scriptcontext.sticky
    except Exception:
        sticky = globals().setdefault("_STICKY", {})
    current = sticky.get(STICKY_KEY)
    if current is None:
        current = sticky[STICKY_KEY] = Controller()
    return current


def show():
    controller().show()


def start():
    """Starts streaming without showing the window, for Rhino's startup commands."""
    current = controller()
    error = current.start()
    if error:
        Rhino.RhinoApp.WriteLine(f"ProjectAR: {error}")
