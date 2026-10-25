# Changelog

All notable changes to the ProjectAR Blender add-on, called ViewAR before 1.2.1. The add-on version is the `version` in `blender/viewar/blender_manifest.toml`. The wire protocol has its own version (`PROTOCOL_VERSION` in `scene_stream.py`).

## 1.3.0 — 2026-10-24

Wire protocol stays at **v1**. The new feature is announced in `hello.features`, so older apps and add-ons keep working together.

### Added
- **Decisions.** The app's `addDecision` keeps a design decision in the scene, in question, options and criteria (QOC) form: the question, the options compared, the one chosen, the criteria that mattered, the reason, an optional confidence from 1 to 5, who made it and when. Variant decisions name their set, material decisions their object and slot.
- **Decision photos.** The photo taken in the room when the decision was made is packed into the .blend file as an image named after the decision, kept with a fake user so it survives saving.
- **Decision log.** A *ProjectAR Decisions* text lists every decision as Markdown, oldest first, and is rewritten whenever one is added or deleted.
- **Decisions sub-panel.** Lists each decision with its choice, the other options, the criteria and the reason. Buttons show the photo in a new Image Editor window, show the chosen variant again, and delete the decision with its photo. *Open Log* opens the text in a new Text Editor window.
- `removeDecision` from the app deletes a decision and its photo.
- **Record Decision** in the Decisions sub-panel records a decision made at the screen, for comparing deciding in Blender with deciding in the room. The dialog asks the same things as the app: a variant set or your own options, the chosen option, criteria (the app's seven suggestions plus your own), a reason and an optional confidence from 1 to 5. An image of the 3D view, taken without the interface before the dialog opens, is kept as its photo. A chosen variant is shown afterwards. Missing answers keep the rest for the next try.
- Decisions have a `source`, `app` or `blender`, shown in the panel and the log. Older records count as `app`.
- **Study sub-panel** for the screen condition of a study. Tasks, counterbalanced orders (the same balanced Latin square as the app, picked from the participant ID's number), a consent check, a task timer and markers, like the app's Study Mode. While a session runs, editing objects and materials, navigating the 3D view, variant switches, decisions, notes, undo, redo, saving and changes from the app are logged with timestamps, edits once per burst. Each session is a folder with `events.csv` and `task-times.csv` in the app's columns, plus a study-wide `ProjectAR Task Times.csv`. Events are appended to disk as they happen, and a session left running when Blender closes or crashes resumes when it opens again.
- **Study Folder** setting.
- **Ask Before Devices Connect** (on by default). A device that hasn't connected before gets nothing, not even the scene's name, and can change nothing, until it's allowed. A popup says a device is asking, and the top of the ProjectAR panel has Allow and Deny. The popup has no buttons, since Blender opens popups under the mouse and a click could land on Allow by accident, and Allow only counts once the request has shown for a second. Allowed devices are remembered by an id the app sends (older apps by IP address) and listed in Settings, where they can be forgotten. A denied device is turned away without asking for a minute, unanswered requests expire after 2 minutes, and at most three wait at once, so requests can't crowd out known devices. Waiting devices get `approval` messages and pings. `hello` lists `pairing`.
- **Export CSV** writes every decision with the app's export columns plus `source`, and the photos in a folder beside the CSV, so both study conditions can be analysed together.
- `hello` lists `decisions` and `textures`.
- **Texture streaming.** Image Texture nodes feeding a Principled BSDF's Base Color, Roughness or Metallic, or its Normal through a Normal Map node, are sent to apps that ask for them (reroutes are followed). Each material lists its texture ids under `textures`, and each texture goes once per device as a new kind 3 frame. PNG and JPEG files that already fit are sent as they are; anything else, or anything larger, is scaled down and sent as PNG, with roughness and metallic maps as grayscale. A base color image's alpha is used when the Alpha input takes it. Texture sending is paced to a few megabytes per tick and at most one new image is encoded per tick, so meshes keep flowing.
- **Texture Size** preference (512, 1024 or 2048 px, default 1024).
- **Scene materials.** A `materials` message lists every material in the file that something uses (up to 200, room scan materials left out), after the full sync and whenever one changes, so the app can try any of them on an object, textures included.
- **Painted images update on devices.** Once painting in Texture Paint (or any pixel edit) pauses for 0.75 s, the image is encoded again from Blender's in-memory pixels and resent under the same texture id, so unsaved paint reaches the app without resending meshes.
- Changes the app makes in the file (decisions, notes, variants, materials, room scans) get one undo step per update, so Ctrl+Z on an earlier edit no longer silently discards them. No step is added when nothing changed, outside Object Mode, or while an operator such as a transform is running.
- The app's `client` message says what it supports. Textures go only to apps that list `textures`, so older apps see no change.

### Changed
- Texture ids include the role (base color, roughness, metallic, normal), so one image used for two inputs is encoded correctly for each.
- Textures wait while a device still has over 8 MB to receive, are resent after a full sync or a dropped backlog, and ids no longer used by any material are forgotten (every used material counts, not only the first 200 offered as a palette). Linked library images are found by their full name.
- Decision images are named after the question without a number, questions are folded onto one line, repeated options are dropped, and hand-edited or damaged decision records are skipped instead of breaking the log and panel.
- Room scan items with a malformed kind are skipped.
- A full sync no longer counts toward the 64 MB backlog limit, so a large scene on slow Wi-Fi finishes instead of being dropped and resent forever. A device that falls behind on updates is still resynced.
- At most 8 devices can connect at once. On Windows the port is opened exclusively, so a second Blender can't silently share it.
- Reading messages from the app uses a growing buffer, so a 4 MB decision with a photo no longer costs quadratic time.
- The largest frame accepted from the app is now 4 MB, up from 1 MB, to fit a decision with its photo.

### Fixed
- A device that reset its connection while being accepted stopped the server from accepting anyone else until Stop and Start. The panel still showed it running.
- Slow but working devices were dropped during a large full sync, then resynced and dropped again, because the 15 s send timeout covered a whole frame. Frames now go in 256 KB chunks, and the timeout applies to each.
- Devices that leave the network without closing are noticed within about 20 s (TCP keepalive), so they no longer hold one of the 8 device places.
- Start no longer succeeds when another program already listens on the same port on 127.0.0.1 (macOS and Linux).
- Messages from the app are applied for at most 50 ms per tick, so a flood can't freeze Blender. Room scans are capped at 500 items, and a scene at 1000 notes.
- NaN, infinite or huge numbers in notes, room scans and material changes are rejected. No message with NaN is ever sent: the app rejected those, so its notes stopped updating.
- JSON `true` is no longer taken as the number 1 for a slot or confidence. Broken Unicode from the app is cleaned instead of failing to save. A room scan with no valid items no longer leaves an empty collection.
- Each scene has its own notes collection and its own decision log, so notes no longer appear in other scenes and one scene's decisions no longer replace another's log. The notes collection is found by a tag, so renaming it is fine.
- A note id that isn't text no longer breaks the Notes panel.
- Hand-edited decisions with odd photo, set, object, slot or confidence values no longer break Export, Delete or Show Photo. Decision ids are cleaned before being used in file names.
- Record Decision picked the wrong variant set or option when two long names (Blender 5 allows 255 bytes) started with the same 63 bytes.
- Objects hidden with H, or in a hidden variant set, no longer show in AR.
- Use Active Collection as Variant Set leaves exactly one option showing, so Blender and the app agree from the start.
- Painted images one or two pixels thin on a side are sent instead of failing to scale.
- While updates are paused, a device that joins no longer pushes variant and note changes to the paused devices.
- Show Chosen Option says so when the set is no longer a variant set. Open Log and Show Photo fall back to naming the text or image when no window can open. A one-off streaming error no longer stays in the panel. Stop is quicker.
- A setMaterial on an object whose mesh is linked from a library is refused instead of failing.

### Tested
- A stress suite of 28 checks: 3,000 fuzzed app messages that must never raise, a corrupted or hand-edited decision store, a deleted log text, 5,000-character unicode questions, textures from a missing file, a float image, an EXR file, a 4096 px image, a 1x1 and a 10x3000 image (with no leftover image copies), non-image node inputs, a device leaving mid-stream and reconnecting, unsaved paint reaching the device, garbage and oversized frames from a client, Blender importing the app's USDZ export with its textures, a texture on the 210th material surviving pruning, and re-selecting a shown variant not counting as a change. A texture suite of 12 checks covers material texture discovery, scaling, orientation, pass-through files, older apps and the material palette.
- Blender 5.2 headless over a real TCP connection, with frames encoded by the app's own Swift code: 17 checks covering the `decisions` feature in `hello`, every QOC field, the packed photo and its name, the log text, duplicate and malformed messages, bad photos and confidence values, the chosen variant applied from the app and from the panel, saving and reopening the file, `removeDecision`, and a 2 MB photo. The panel's drawing code was also run against a stand-in layout.

## 1.2.1 — 2026-10-21

Wire protocol stays at **v1**, and nothing changes for the app's connection.

### Changed
- **Renamed to ProjectAR**, to match the app. The sidebar tab, panel, extension name, Bonjour name shown in the app, Notes collection and scan materials now say ProjectAR. The zip is now `projectar-<version>.zip`.
- Internal names stay the same, so saved files keep their settings: the extension id `viewar`, operator and property names, note properties, and the `_viewar._tcp` Bonjour service type the app looks for. Notes in a file's existing *ViewAR Notes* collection still work.

## 1.2.0 — 2026-10-19

Wire protocol stays at **v1**. New features are announced in `hello.features`, so this release works with older ViewAR apps, and the new app works with older add-ons.

### Added
- **Messages from the app.** Each device connection now also reads frames. JSON messages are applied on Blender's main thread at the start of each tick. Unknown types are ignored, and a client that sends a frame over 1 MB is disconnected.
- **Variant sets.** Mark a collection as an *AR Variant Set* (Variants sub-panel) and each child collection is a design option. Every option is streamed even while hidden, including options in excluded collections, so the app can switch instantly. The app's `setVariant` and the panel's option buttons show one option and hide the others. A `variants` message goes to devices after the full sync and whenever a set changes.
- **Notes.** The app's `addNote` creates an empty in the *ViewAR Notes* collection at the spot on the model, named after the note and showing its name in the viewport. Notes can be selected, edited, deleted or added at the 3D cursor in the new Notes sub-panel. A `notes` message goes to devices after the full sync and whenever a note changes.
- **`setMaterial`.** Materials the app tries on with *Also Apply in Blender* are assigned to the slot. The app already sent this message; the add-on now applies it.
- **Room scans.** The app's `addRoomScan` builds a scanned room as named boxes (walls, doors, windows, openings, furniture by category, and floor outlines) in a new *Room Scan* collection with Walls, Openings, Furniture and Floors sub-collections and simple materials. Scans sent while the scene is placed at real size line up with the model. Scan objects have Show in AR turned off.
- `hello` lists `features`: `setMaterial`, `variants`, `notes`, `roomScan`.

### Tested
- Blender 5.2.2 LTS headless over a real TCP connection: 52 checks covering room scans (positions, rotation, sizes, floor polygons, naming, sub-collections, bad items, not streamed back), features in `hello`, variant streaming and switching from both sides (including excluded collections), note round trips, edits and deletes in either direction, duplicate and malformed messages, `setMaterial`, a second device's full sync, and oversized frames. The iOS app's own Swift encoder and decoder were also checked against the running add-on.

## 1.1.0 — 2026-10-09

Wire protocol stays at **v1**, so this release works with the existing ViewAR iOS app.

### Added
- **Instances.** Collection, particle and Geometry Nodes instances are streamed. Each one is sent as its own object with a stable id, and a mesh shared by many instances is built only once per update. Toggle it with **Include Instances** and cap it with **Max Instances** (default 500).
- **Choose what to stream.** Visible objects, selected objects, or a single collection.
- **Show in AR** per object, to keep reference or helper objects out of AR while they stay visible in Blender. It is saved with the .blend file, and Alt+click applies it to all selected objects.
- **AR Origin.** Use the world origin or the 3D cursor as the point placed in AR.
- **Pause Updates.** Freezes what devices show during heavy edits. Devices catch up on resume, and a device that joins while paused still gets the current scene.
- **Saved preferences.** Port, update rate, triangle and instance limits are now add-on preferences, kept between sessions and also editable under Edit › Preferences › Add-ons.
- **Start Server with Blender** option.
- **Copy Address** button, which works with Universal Clipboard on iPhone.
- All LAN addresses are listed in the panel and advertised over Bonjour, which helps with VPNs and machines on both Ethernet and Wi-Fi.
- Per-device data counter and **Disconnect** button.
- **Statistics** and **Settings** sub-panels, and the most recent error shown in the panel.
- `CHANGELOG.md`, `build.py`, and a ready-to-install zip in `dist/`.

### Changed
- Changing the Unit Scale or switching the active scene resyncs devices automatically. Before, it needed a manual Resync.
- Changing **Max Triangles per Object** while streaming applies immediately.
- Material edits now update objects that live outside the scene, such as sources of instanced collections.
- Idle ticks are about 2.5× cheaper, because converted matrices are cached until an object moves.

### Fixed
- Curve, text, surface and metaball objects are not sent twice when instances are enabled. Blender 5 also lists their evaluated mesh as an instance of themselves.
- No `Material.use_nodes` deprecation warning on Blender 5.
- `unitScale` is rounded, so 0.01 is sent as `0.01` and not `0.009999999776`.
- README used the old `extension validate --source-dir` syntax, which Blender 5.2 rejects.

### Tested
- Blender 5.2.2 LTS (Python 3.13) on macOS: a headless suite of 47 checks over a real TCP connection, covering full sync, live edits, instances, filters, origin, pause, budgets, resync triggers, devices, discovery, panel drawing, the busy-port error, auto start, and disable/re-enable.

## 1.0.0 — 2026-10-08

First release, built from the specification in `ProjectAR-Blender-Addon.md`.

- TCP server with length-prefixed frames and one sender thread per device.
- Live streaming of transforms and evaluated meshes, with Principled BSDF materials.
- Optional Bonjour discovery with bundled pure-Python `zeroconf`, `ifaddr` and `async_timeout` wheels.
- Material changes made in the Shader Editor are picked up through the material's node tree.
