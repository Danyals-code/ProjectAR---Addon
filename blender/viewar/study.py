# SPDX-License-Identifier: GPL-3.0-or-later
"""Study sessions for the screen condition: the participant works in Blender
instead of in the room with the app.

The researcher sets up the same tasks as in the app's Study Mode, and each
participant gets the same counterbalanced order (a balanced Latin square). Task
times, markers and what the participant does in Blender (moving and editing
objects, materials, variants, decisions, notes, navigating the 3D view, undo,
saving) are logged with timestamps.

Each session is a folder in the study folder:
    session.json   participant, order, task times (rewritten on each change)
    events.jsonl   one event per line, appended as it happens
    events.csv, task-times.csv   written when the session ends
The study folder also gets "ProjectAR Task Times.csv" over every session. The
CSV columns match the app's exports, so both conditions can be analysed together.
"""

import csv
import datetime
import io
import json
import os
import re
import time
import uuid

import bpy

FOLDER_NAME = "ProjectAR Study"
ACTIVE_FILE = "active-session.txt"
ALL_TASK_TIMES = "ProjectAR Task Times.csv"

EVENT_COLUMNS = ("session_id", "participant", "condition", "t_seconds", "timestamp", "event", "detail")
TASK_COLUMNS = ("session_id", "participant", "condition", "order_number", "task_order",
                "position", "task", "started", "ended", "duration_s")

# Edits closer together than this count as one burst, logged once.
BURST_GAP = 0.75
MAX_TEXT = 500

_active = None
# (kind, name) -> [first seen, last seen, updates]
_bursts = {}
_last_view = None


# ---------------------------------------------------------------- task orders


def task_names(text):
    """Tasks typed as a comma or line separated list, without blanks or repeats."""
    parts = re.split(r"[,\n]", text or "")
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def balanced_row(size, row):
    """One row of a balanced Latin square (Williams design) as indexes, the
    same as the app's. For an odd size, rows size to 2 size - 1 are the first
    ones reversed."""
    if size <= 0:
        return []
    row %= size * 2
    result, low, high = [], 0, 0
    for i in range(size):
        if i < 2 or i % 2:
            value = low
            low += 1
        else:
            value = size - high - 1
            high += 1
        result.append((value + row) % size)
    if size % 2 and row >= size:
        result.reverse()
    return result


def order_count(names, counterbalance):
    n = len(names)
    if not counterbalance or n < 2:
        return 1
    return n if n % 2 == 0 else n * 2


def task_order(names, counterbalance, row):
    if not counterbalance or len(names) < 2:
        return list(names)
    return [names[i] for i in balanced_row(len(names), row)]


def suggested_row(participant, sessions_so_far, count):
    """The participant ID's trailing number picks the order, so P01 gets the
    first and P07 the seventh. Without a number, the sessions so far do."""
    match = re.search(r"(\d+)\s*$", participant or "")
    number = max(int(match.group(1)) - 1, 0) if match and len(match.group(1)) < 18 else sessions_so_far
    return number % max(count, 1)


# ---------------------------------------------------------------- files


def default_folder():
    return os.path.join(os.path.expanduser("~"), "Documents", FOLDER_NAME)


def _iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe(text):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-")
    return cleaned[:40] or "Participant"


def _write_atomic(path, text):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(temp, path)


def sessions_in(folder):
    """Session folders in the study folder, oldest first."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if os.path.isfile(os.path.join(folder, n, "session.json"))]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- session


class Session:
    def __init__(self, folder, header):
        self.folder = folder
        self.header = header

    @property
    def id(self):
        return self.header["id"]

    @property
    def timings(self):
        return self.header.setdefault("task_timings", [])

    @property
    def running_task(self):
        timings = self.timings
        return timings[-1] if timings and timings[-1].get("end") is None else None

    @property
    def next_task(self):
        done = len(self.timings)
        order = self.header.get("task_order") or []
        return order[done] if done < len(order) else f"Task {done + 1}"

    def elapsed(self, now=None):
        return (now or time.time()) - self.header["started"]

    def save(self):
        _write_atomic(os.path.join(self.folder, "session.json"), json.dumps(self.header, ensure_ascii=False, indent=2))

    def append(self, kind, detail):
        now = time.time()
        event = {"t": round(now - self.header["started"], 3), "time": _iso(now), "kind": kind,
                 "detail": {str(k): str(v)[:MAX_TEXT] for k, v in (detail or {}).items()}}
        with open(os.path.join(self.folder, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.header["event_count"] = self.header.get("event_count", 0) + 1

    def events(self):
        """Every event read back, skipping a line cut short by a crash."""
        out = []
        try:
            with open(os.path.join(self.folder, "events.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and "kind" in event:
                        out.append(event)
        except OSError:
            pass
        return out


def active():
    return _active


def start(folder, participant, condition, notes, names, counterbalance, row, consent=True):
    """Starts a session and returns it. Ends any running one first."""
    global _active, _last_view
    if _active is not None:
        end()
    participant = " ".join((participant or "").split())[:80]
    if not participant:
        raise ValueError("A participant ID is needed")
    os.makedirs(folder, exist_ok=True)
    started = time.time()
    session_id = str(uuid.uuid4()).upper()
    stamp = datetime.datetime.fromtimestamp(started).strftime("%Y-%m-%d %H-%M-%S")
    path = os.path.join(folder, f"{stamp} {_safe(participant)} {session_id[:8]}")
    os.makedirs(path, exist_ok=False)
    order = task_order(names, counterbalance, row)
    header = {
        "id": session_id,
        "participant": participant,
        "condition": " ".join((condition or "").split())[:80],
        "notes": (notes or "")[:2000],
        "started": started,
        "ended": None,
        "device": "Blender",
        "blender_version": bpy.app.version_string,
        "file": bpy.data.filepath,
        "consent_confirmed": bool(consent),
        "task_order": order,
        "order_number": row + 1 if counterbalance and len(names) > 1 else None,
        "task_timings": [],
        "event_count": 0,
    }
    _active = Session(path, header)
    _bursts.clear()
    _last_view = None
    detail = {"consent_confirmed": str(bool(consent)).lower(), "file": os.path.basename(bpy.data.filepath) or "unsaved"}
    if order:
        detail["task_order"] = " > ".join(order)
        if header["order_number"]:
            detail["order_number"] = header["order_number"]
    _active.append("session.start", detail)
    _active.save()
    _write_active_pointer(folder, path)
    return _active


def log(kind, detail=None):
    """Adds an event to the running session. Does nothing without one, and
    never raises: logging must not break what's being logged."""
    if _active is None:
        return
    try:
        _active.append(kind, detail)
    except OSError as exc:
        print(f"ProjectAR: could not log a study event: {exc}")


def start_task():
    session = _active
    if session is None or session.running_task is not None:
        return None
    timing = {"name": session.next_task, "position": len(session.timings) + 1, "start": time.time(), "end": None}
    session.timings.append(timing)
    log("task.start", {"task": timing["name"], "position": timing["position"]})
    session.save()
    return timing


def end_task(reason=None):
    session = _active
    timing = session.running_task if session is not None else None
    if timing is None:
        return None
    flush_bursts(force=True)
    timing["end"] = time.time()
    detail = {"task": timing["name"], "position": timing["position"],
              "duration_ms": round((timing["end"] - timing["start"]) * 1000)}
    if reason:
        detail["ended_by"] = reason
    log("task.end", detail)
    session.save()
    return timing


def add_marker(label):
    label = " ".join((label or "").split())[:200] or "Marker"
    log("marker", {"label": label})


def end():
    """Ends the running session and writes its CSVs. Returns its folder."""
    global _active
    session = _active
    if session is None:
        return None
    end_task("session.end")
    flush_bursts(force=True)
    log("session.end")
    session.header["ended"] = time.time()
    session.save()
    _active = None
    _bursts.clear()
    folder = os.path.dirname(session.folder)
    _write_active_pointer(folder, None)
    write_csvs(session)
    write_all_task_times(folder)
    return session.folder


def _write_active_pointer(folder, path):
    pointer = os.path.join(folder, ACTIVE_FILE)
    try:
        if path:
            _write_atomic(pointer, path)
        elif os.path.exists(pointer):
            os.remove(pointer)
    except OSError as exc:
        print(f"ProjectAR: could not update {pointer}: {exc}")


def resume(folder):
    """Picks up a session left running when Blender closed or crashed."""
    global _active
    if _active is not None:
        return _active
    try:
        with open(os.path.join(folder, ACTIVE_FILE), encoding="utf-8") as f:
            path = f.read().strip()
    except OSError:
        return None
    header = _read_json(os.path.join(path, "session.json"))
    if not header or header.get("ended") or not isinstance(header.get("started"), (int, float)) or not header.get("id"):
        _write_active_pointer(folder, None)
        return None
    if not isinstance(header.get("task_timings"), list):
        header["task_timings"] = []
    _active = Session(path, header)
    log("session.resume", {"file": os.path.basename(bpy.data.filepath) or "unsaved"})
    return _active


# ---------------------------------------------------------------- CSV


def _csv_text(rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(rows)
    return out.getvalue()


def _timing_rows(header):
    rows = []
    order = " > ".join(header.get("task_order") or [])
    for timing in header.get("task_timings") or []:
        start, end_ = timing.get("start"), timing.get("end")
        rows.append([
            header["id"], header.get("participant", ""), header.get("condition", ""),
            header.get("order_number") or "", order, timing.get("position", ""), timing.get("name", ""),
            _iso(start) if isinstance(start, (int, float)) else "",
            _iso(end_) if isinstance(end_, (int, float)) else "",
            f"{end_ - start:.3f}" if isinstance(start, (int, float)) and isinstance(end_, (int, float)) else "",
        ])
    return rows


def write_csvs(session):
    """events.csv and task-times.csv in the session's folder."""
    header = session.header
    lead = [header["id"], header.get("participant", ""), header.get("condition", "")]
    rows = [EVENT_COLUMNS]
    for event in session.events():
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        rows.append(lead + [
            f"{float(event.get('t', 0)):.3f}", event.get("time", ""), event.get("kind", ""),
            "; ".join(f"{k}={detail[k]}" for k in sorted(detail)),
        ])
    _write_atomic(os.path.join(session.folder, "events.csv"), _csv_text(rows))
    _write_atomic(os.path.join(session.folder, "task-times.csv"), _csv_text([TASK_COLUMNS] + _timing_rows(header)))


def write_all_task_times(folder):
    """Task times over every session in the study folder."""
    headers = [_read_json(os.path.join(path, "session.json")) for path in sessions_in(folder)]
    headers = [h for h in headers if h and h.get("id") and isinstance(h.get("started"), (int, float))]
    rows = [TASK_COLUMNS]
    for header in sorted(headers, key=lambda h: h["started"]):
        rows += _timing_rows(header)
    path = os.path.join(folder, ALL_TASK_TIMES)
    try:
        _write_atomic(path, _csv_text(rows))
    except OSError as exc:
        print(f"ProjectAR: could not write {path}: {exc}")
        return None
    return path


# ---------------------------------------------------------------- activity


_APP_DETAIL_KEYS = ("set", "option", "object", "slot", "material", "question", "chosen", "text", "id")


def log_app_message(message):
    """A change the app made in the scene, such as a variant switch."""
    if _active is None:
        return
    detail = {k: message[k] for k in _APP_DETAIL_KEYS if isinstance(message.get(k), (str, int)) and not isinstance(message.get(k), bool)}
    log("app." + str(message.get("type", "message"))[:40], detail)


def note_edit(kind, name):
    """Counts an edit toward a burst, logged once it pauses."""
    now = time.monotonic()
    burst = _bursts.get((kind, name))
    if burst is None:
        _bursts[(kind, name)] = [now, now, 1]
    else:
        burst[1] = now
        burst[2] += 1


def flush_bursts(force=False):
    if _active is None:
        _bursts.clear()
        return
    now = time.monotonic()
    for key, (first, last, count) in sorted(_bursts.items(), key=lambda item: item[1][0]):
        if force or now - last >= BURST_GAP:
            del _bursts[key]
            kind, name = key
            detail = {"updates": count, "duration_ms": round((last - first) * 1000)}
            if name:
                detail["object" if kind.startswith("object.") else "name"] = name
            log(kind, detail)


def on_depsgraph_update(scene, depsgraph):
    """Edits the participant makes, from Blender's update notifications."""
    if _active is None:
        return
    screen = bpy.context.screen
    if screen is not None and screen.is_animation_playing:
        return
    for update in depsgraph.updates:
        data = update.id
        if isinstance(data, bpy.types.Object):
            if update.is_updated_transform:
                note_edit("object.transform", data.name)
            if update.is_updated_geometry:
                note_edit("object.geometry", data.name)
        elif isinstance(data, bpy.types.Material) and update.is_updated_shading:
            note_edit("material.edit", data.name)


def _view_state():
    """The largest 3D view's camera, rounded so jitter doesn't count."""
    best = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D" and (best is None or area.width * area.height > best.width * best.height):
                best = area
    if best is None:
        return None
    r3d = best.spaces.active.region_3d
    location = tuple(round(v, 3) for v in r3d.view_location)
    rotation = tuple(round(v, 3) for v in r3d.view_rotation)
    return location, rotation, round(r3d.view_distance, 3), r3d.view_perspective


def tick():
    """Run about twice a second while a session is active."""
    global _last_view
    if _active is None:
        return
    if not bpy.app.background:
        try:
            view = _view_state()
        except (AttributeError, ReferenceError):
            view = None
        if view is not None and _last_view is not None and view != _last_view:
            note_edit("view.navigate", "")
        _last_view = view
    flush_bursts()
