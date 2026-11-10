# SPDX-License-Identifier: GPL-3.0-or-later
"""Design decisions, made in the room with the app or at the screen in Rhino.

Each is kept in question, options and criteria (QOC) form: what was decided,
the options compared, the one chosen, what mattered, and why. Records, the
log and the CSV export are the Blender add-on's, so decisions from either
program, and the app's own export, can be put together.

Where they're kept is the store's business (rhino_host keeps them in the
document's user text, photos included, so they travel with the .3dm file):

    store.load() -> str or None     store.save(text)
    store.photo(id) -> bytes        store.set_photo(id, bytes)    store.remove_photo(id)
"""

import base64
import binascii
import datetime
import json
import os
import re
import uuid

from .protocol import as_int, clean, one_line

DECISION_KINDS = ("variant", "material", "custom")
# Where a decision was made: in the room with the app, or at the screen. The
# Blender add-on writes "blender"; Rhino writes "rhino".
DECISION_SOURCES = ("app", "blender", "rhino")
SCREEN_SOURCE = "rhino"
# The app's suggestions, so both places offer the same criteria.
SUGGESTED_CRITERIA = (
    "Fits the space",
    "Size and proportion",
    "Looks right in this light",
    "Material and finish",
    "Room to move around it",
    "Goes with the room",
    "Cost",
)
MAX_DECISION_TEXT = 2000
MAX_DECISION_OPTIONS = 50
# Base64 of the photo. The app sends JPEGs of about 300 KB.
MAX_PHOTO_CHARS = 3 * 1024 * 1024


def decisions(store):
    """The document's decisions, oldest first."""
    raw = store.load()
    if not raw:
        return []
    try:
        items = json.loads(str(raw))
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    # The text can be edited by hand, so only well-formed records count.
    out = []
    for d in items:
        if not isinstance(d, dict):
            continue
        if not all(isinstance(d.get(k), str) and d.get(k) for k in ("id", "question", "chosen")):
            continue
        for k in ("options", "criteria"):
            if not isinstance(d.get(k), list):
                d[k] = []
            d[k] = [str(v) for v in d[k]]
        for k in ("reason", "author", "created"):
            if not isinstance(d.get(k), str):
                d[k] = ""
        for k in ("set", "object", "kind"):
            if k in d and not isinstance(d[k], str):
                del d[k]
        if "slot" in d and as_int(d["slot"]) is None:
            del d["slot"]
        if "confidence" in d and not (as_int(d["confidence"]) is not None and 1 <= d["confidence"] <= 5):
            del d["confidence"]
        if d.get("source") not in DECISION_SOURCES:
            d["source"] = "app"
        out.append(d)
    return out


def find_decision(store, decision_id):
    return next((d for d in decisions(store) if d["id"] == decision_id), None)


def _save(store, items):
    store.save(json.dumps(items, ensure_ascii=False))


def add_decision(store, message, source="app"):
    """Keeps a decision from the app, or one recorded at the screen. Returns
    it, or None if it's not valid or already kept."""
    decision_id = clean(message.get("id"), 64)
    question = one_line(message.get("question"), MAX_DECISION_TEXT)
    chosen = one_line(message.get("chosen"), 200)
    options = message.get("options")
    if not decision_id or not question or not chosen or not isinstance(options, list):
        return None
    if find_decision(store, decision_id) is not None:
        return None
    options = [one_line(o, 200) for o in options[:MAX_DECISION_OPTIONS] if one_line(o, 200)]
    options = list(dict.fromkeys(options))  # no repeats
    if chosen not in options:
        options.append(chosen)
    criteria = message.get("criteria")
    criteria = [clean(c, 200) for c in criteria[:MAX_DECISION_OPTIONS]] if isinstance(criteria, list) else []
    kind = message.get("kind") if message.get("kind") in DECISION_KINDS else "custom"
    confidence = as_int(message.get("confidence"))
    if not (confidence is not None and 1 <= confidence <= 5):
        confidence = None

    items = decisions(store)
    decision = {
        "id": decision_id,
        "created": clean(message.get("created"), 40)
        or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "author": clean(message.get("author"), 80),
        "question": question,
        "kind": kind,
        "options": options,
        "chosen": chosen,
        "criteria": [c for c in criteria if c],
        "reason": clean(message.get("reason"), MAX_DECISION_TEXT),
        "source": source if source in DECISION_SOURCES else "app",
    }
    if kind == "variant" and message.get("set"):
        decision["set"] = clean(message.get("set"), 200)
    if kind == "material" and message.get("object"):
        decision["object"] = clean(message.get("object"), 200)
        if as_int(message.get("slot")) is not None:
            decision["slot"] = message["slot"]
    if confidence is not None:
        decision["confidence"] = confidence

    photo = message.get("photo")
    if isinstance(photo, str) and 0 < len(photo) <= MAX_PHOTO_CHARS:
        try:
            data = base64.b64decode(photo, validate=True)
        except (binascii.Error, ValueError):
            data = None
        if data and data[:3] == b"\xff\xd8\xff":  # a JPEG, as the app sends
            store.set_photo(decision_id, data)
            decision["photo"] = photo_file_name(decision)

    items.append(decision)
    _save(store, items)
    return decision


def record_decision(store, question, options, chosen, reason, *,
                    set_name="", criteria=(), confidence=None, author="Rhino", photo=None):
    """Records a decision made at the screen, stored exactly like one from
    the app. photo is JPEG bytes. Returns the decision, or None if it's not
    valid. Showing a chosen variant is the caller's job."""
    message = {
        "id": str(uuid.uuid4()).upper(),  # like the app's ids
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "author": author or "Rhino",
        "question": question,
        "kind": "variant" if set_name else "custom",
        "options": list(options),
        "chosen": chosen,
        "criteria": list(criteria),
        "reason": reason,
        "confidence": confidence,
    }
    if set_name:
        message["set"] = set_name
    if photo:
        message["photo"] = base64.b64encode(photo).decode("ascii")
    return add_decision(store, message, source=SCREEN_SOURCE)


def remove_decision(store, decision_id):
    items = decisions(store)
    if not any(d["id"] == decision_id for d in items):
        return False
    store.remove_photo(decision_id)
    _save(store, [d for d in items if d["id"] != decision_id])
    return True


def _where(decision):
    if decision.get("source") == "rhino":
        return "in Rhino"
    if decision.get("source") == "blender":
        return "in Blender"
    return "in the room"


def decisions_markdown(scene_name, items):
    """The log as Markdown, one QOC entry per decision, oldest first."""
    lines = [
        f"# Decisions for {scene_name}",
        "",
        "Made in the room with ProjectAR or at the screen in Rhino. Each one lists",
        "the question, the options compared, the one chosen, the criteria that",
        "mattered and the reason.",
    ]
    for number, d in enumerate(items, start=1):
        lines += ["", f"## {number}. {d['question']}", ""]
        lines.append(f"- Options: {', '.join(d.get('options', []))}")
        lines.append(f"- Chosen: {d.get('chosen', '')}")
        if d.get("set"):
            lines.append(f"- Variant set: {d['set']}")
        if d.get("object"):
            slot = d.get("slot")
            where = f"{d['object']}, slot {slot + 1}" if isinstance(slot, int) else d["object"]
            lines.append(f"- Material slot: {where}")
        if d.get("criteria"):
            lines.append(f"- Criteria: {', '.join(d['criteria'])}")
        lines.append(f"- Reason: {d.get('reason', '')}")
        if d.get("confidence"):
            lines.append(f"- Confidence: {d['confidence']} of 5")
        if d.get("photo"):
            lines.append("- Photo: kept with the document")
        lines.append(f"- Made {_where(d)}")
        lines.append(f"- By {d.get('author') or 'unknown'}, {d.get('created', '')}")
    return "\n".join(lines) + "\n"


CSV_COLUMNS = (
    "id", "created", "scene", "author", "question", "kind", "variant_set", "object", "slot",
    "options", "chosen", "criteria", "reason", "confidence", "photo", "source",
)


def _csv_field(value):
    value = str(value)
    if any(c in value for c in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def safe_file_id(decision_id):
    """A decision id as it may appear in a file name."""
    return re.sub(r"[^A-Za-z0-9-]", "", decision_id)[:36] or "decision"


def photo_file_name(decision):
    """As the app names photos in its export."""
    return f"decision-{safe_file_id(decision['id'])[:8].lower()}.jpg"


def decisions_csv(scene_name, items, has_photo):
    """The log as CSV, with the app's export columns and then the source,
    so the two can be put together."""
    rows = [",".join(CSV_COLUMNS)]
    for d in items:
        slot = d.get("slot")
        rows.append(",".join(_csv_field(v) for v in (
            d["id"],
            d.get("created", ""),
            scene_name,
            d.get("author", ""),
            d["question"],
            d.get("kind", "custom"),
            d.get("set", ""),
            d.get("object", ""),
            slot if isinstance(slot, int) else "",
            " | ".join(d.get("options", [])),
            d["chosen"],
            " | ".join(d.get("criteria", [])),
            d.get("reason", ""),
            d.get("confidence") or "",
            photo_file_name(d) if has_photo(d) else "",
            d.get("source", "app"),
        )))
    return "\n".join(rows) + "\n"


def export_decisions(store, scene_name, csv_path):
    """Writes the CSV and, in a folder beside it, each decision's photo.
    Returns the number of decisions and of photos written."""
    items = decisions(store)
    photos = {d["id"]: store.photo(d["id"]) for d in items}
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write(decisions_csv(scene_name, items, lambda d: bool(photos.get(d["id"]))))
    folder = os.path.splitext(csv_path)[0] + " Photos"
    written = 0
    for d in items:
        data = photos.get(d["id"])
        if not data:
            continue
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, photo_file_name(d)), "wb") as f:
            f.write(data)
        written += 1
    return len(items), written
