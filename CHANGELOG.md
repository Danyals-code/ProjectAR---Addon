# Changelog

All notable changes to the ProjectAR Blender add-on, called ViewAR before 1.2.1. The add-on version is the `version` in `blender/viewar/blender_manifest.toml`. The wire protocol has its own version (`PROTOCOL_VERSION` in `scene_stream.py`).

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
