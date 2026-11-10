# ProjectAR for Rhino (Add-on)

Live preview your Rhino model in augmented reality on iPhone and iPad.

The add-on runs a small server inside Rhino and streams the open document over your local network to the **ProjectAR** iOS app, the same app the Blender add-on streams to. Move an object, edit it, or change a material, and the AR view updates within a fraction of a second. It speaks the same wire protocol as the Blender add-on, so the app needs no changes.

> **Status: 0.1.0, not yet run in Rhino.** It was written without a copy of Rhino available, against the RhinoCommon API. Everything that doesn't need Rhino (the protocol, the server, streaming, snapshots, study sessions, decisions) is tested end to end with a stand-in for the app. The Rhino side is tested against a stand-in for RhinoCommon. The first run in Rhino 8 is the real test: see [First run in Rhino](#first-run-in-rhino).

---

## Features

- **Live streaming.** Breps, extrusions, surfaces, meshes and SubDs are sent as their render meshes, the same ones Rhino shades with.
- **Moves cost almost nothing.** Rhino rewrites an object's geometry when you move it. The add-on notices moves, rotations and uniform scales, and sends just a new matrix instead of the whole mesh. Other edits send the mesh again, at most 10 times a second per object.
- **Blocks.** Every object in a block, nested blocks included, is streamed as an instance. Current apps get each block object's mesh once, so a hundred chairs cost one chair. Older apps get every instance as its own mesh.
- **Materials.** Physically based materials send base color, metallic, roughness and alpha. Older Rhino materials send diffuse color, transparency and shine. Objects without a material show their display color (layer or object color), which is how most Rhino models are colored. Image textures on base color, roughness and metallic are sent too.
- **Units.** Millimeters, inches or meters: everything goes to the app in meters, as the Blender add-on does, so the model lands at real size, and lights reach and shine as they would in a real room (the app's lights reach a fixed 20 units and its brightness assumes meters).
- **Choose what to stream.** Visible objects, only the selection, or one layer and its sublayers. **Keep Selected Out** keeps helper objects out of AR while they stay visible in Rhino.
- **AR origin.** Use the world origin, or pick a point (or the bottom center of the selection), so a model far from the origin still lands where you tap.
- **Pause updates** while you make heavy edits. Devices catch up when you resume.
- **Automatic discovery.** Rhino shows up in the app under *Nearby* as "ProjectAR Rhino on *computer*". You can also type the address.
- **Several devices** at once. A device that joins late gets the whole document.
- **Asks before devices connect.** A device you haven't allowed gets nothing until you press Allow, and allowed devices are remembered.
- **Design variants.** Make a parent layer a variant set, and each of its sublayers becomes a design option. Switch options in the app or in the window, and the other side follows.
- **Notes from AR.** Notes left on the model in the app arrive as text dots on a *ProjectAR Notes* layer. Edit or delete them in Rhino, or add your own above the selection, and every device updates.
- **Material try-on.** Materials picked in the app with *Also Apply in Rhino* are assigned to the object. Looks made in the app arrive as physically based materials, made once and updated when you edit the look in the app. A material picked on a block instance changes the object inside the block.
- **Lights and cameras.** Point, spot, directional and rectangular lights, and the sun, go to the app. The active view and named views go as cameras.
- **Camera from the phone.** The app can move the active view's camera to where the phone is, so you frame a view by walking around the model.
- **USDZ snapshots.** **Send Snapshot** (or the app) sends what's streamed as a USDZ file, with materials and textures, and the app saves it to its Library.
- **Room scans.** A room scanned in the app (LiDAR iPhone or iPad) arrives as named boxes (Wall 1, Door 1, Table 1…) on a *Room Scan* layer, kept out of AR.
- **Decisions.** Decisions made in the room arrive with their photo, options, criteria and reason. Record your own at the screen with **Record Decision**, with an image of the view. Both are kept in the .3dm file, and export to CSV with the same columns as the app's and the Blender add-on's.
- **Study sessions at the screen.** The same tasks, counterbalanced orders and CSV files as the app's Study Mode and the Blender add-on. The log covers moving and editing objects, materials, layers, variants, notes, decisions, commands, navigating the view, undo and saving.

**Not streamed:** curves, points, hatches, annotations and point clouds, which have no surface to show.

---

## Requirements

| Item | Requirement |
|---|---|
| Rhino | Rhino 8 on Windows or macOS. It needs Rhino 8's Python 3, so Rhino 7 won't run it |
| iOS | The ProjectAR app on an ARKit-capable iPhone or iPad. Variants, notes, try-on and room scans need app 1.2 or later; instancing, lights, cameras and snapshots need 1.4 |
| Network | Computer and phone on the **same** Wi-Fi or LAN, without client isolation |

No packages need installing. The Bonjour libraries for automatic discovery are bundled as pure Python wheels, and Python reads them directly.

---

## Installation

1. Take `dist/projectar-rhino-<version>.zip` from this folder (or build it, see below) and unzip it anywhere. It holds a *ProjectAR for Rhino* folder.
2. In Rhino, type `RunPythonScript`, and pick `ProjectAR.py` in that folder.
3. The ProjectAR window opens.

To open it with one word, add an alias in **Options › Aliases**: `ProjectAR` → `! _-RunPythonScript "<folder>/ProjectAR.py"`.

To start streaming every time Rhino opens, turn on **Start Server When ProjectAR Opens** in the window's Settings, and add the same `_-RunPythonScript` line to **Options › General › Command Lists** (run these commands every time Rhino starts).

### Building the zip

```bash
python3 "Rhino Add-on/build.py" --test
```

This runs the tests and writes `Rhino Add-on/dist/projectar-rhino-<version>.zip`, taking the version from `projectar/__init__.py`.

---

## Usage

1. Run ProjectAR. The window has five tabs.
2. In **Connect**, press **Start Server**. The tab shows this computer's address and whether nearby devices can see it.
3. Open the ProjectAR app and pick *ProjectAR Rhino on …* under **Nearby**, or enter the address.
4. The first time a device connects, the window asks. Press **Allow**. Allowed devices connect straight away next time, and are listed in **Settings › Allowed Devices**.
5. Tap a surface in the app to place the model. You see it from the front (Rhino's Front view) first.
6. Work as usual.

Closing the window doesn't stop the server. Run ProjectAR again to bring it back, and **Stop** in Connect to stop.

### Stream

| Option | What it does |
|---|---|
| **Visible Objects / Selected Objects / Layer** | What to send. *Layer* streams one layer and its sublayers. |
| **AR origin** | *World Origin*, or *Picked Point*. **Use Selection** puts the origin at the bottom center of the selection, so the model stands on the floor where you tap. **Pick Point** picks one in a view. |
| **Include Blocks** | Stream block instances, up to **Max block instances** (Settings). |
| **Stream Lights and Cameras** | Send lights, the active view and named views to the app. |
| **Pause Updates** | Freezes what devices show. **Resume Updates** sends everything that changed. |
| **Send Snapshot** | Sends what's streamed as a USDZ file to every connected device that can save one. |
| **Keep Selected Out / Show Selected** | Keeps the selected objects out of AR while they stay visible in Rhino, for reference planes and helpers. Saved in the object's user text (`ProjectAR.HideInAR`). |

**Statistics** shows what's streamed, warns when the model is heavy for older iPhones, and lists objects over the triangle limit.

### Review

**Variants.** Put each design option on its own sublayer of one parent layer (for example *Sofa* › *Option A*, *Option B*), make the parent the current layer, and press **Use Current Layer as Variant Set**. The options are listed with the shown one marked ●. Click one to show it and hide the others, or switch in the app. Hidden options are still streamed, so switching in the app is instant. **Stop Using** turns the layer back into an ordinary one. Variant sets are saved in the document.

**Notes.** Lists every note with its author. **Select** selects its dot, **Delete** removes it. Type a note and press **Add Note at Selection** to leave one above the selected object, pointing at it.

**Decisions.** **Record Decision** takes an image of the active view, then asks what you decided: a variant set (its options are listed, the shown one picked) or your own comma-separated options, the option chosen, what mattered (the app's seven criteria, plus your own), the reason, and how sure you are from 1 to 5. A chosen variant is shown afterwards. Each decision is listed with buttons to show its photo, show the chosen variant again, or delete it. **Open Log** shows every decision as text, and **Export CSV** saves them with the photos in a folder beside the file.

### Study

For the screen condition of a study, where participants work in Rhino instead of in the room. Type the **Tasks** once (for example *Oak, Walnut, Glass*), enter the **Participant ID**, confirm **Participant Agreed to Take Part** and press **Start Session**. Then **Start** and **End** each task. **End Session** writes the files. Each session is a folder in the study folder (Documents/ProjectAR Study unless changed in Settings) with `events.csv` and `task-times.csv`, and the study folder gets `ProjectAR Task Times.csv` over every session. Participant P07 gets the same task order as in the app and in Blender.

### Settings

Port (51515, as in Blender), updates per second, max triangles per object, max block instances, texture size, starting the server with the window, asking before devices connect, allowed devices, and the study folder. Settings are kept in `ProjectAR/rhino-settings.json` in your app data folder (`%APPDATA%` on Windows, `~/Library/Application Support` on macOS).

---

## How Rhino maps to the app

| In Rhino | In the app |
|---|---|
| Brep, extrusion, surface, mesh, SubD | An object, named by its Rhino name. Unnamed ones are named by type: *Polysurface*, *Polysurface 2*… |
| Block instance | One instance per object in the block, `instance/object#n`, sharing a prototype mesh |
| Layer set as a variant set | A variant set; its sublayers are the options |
| Text dot on the ProjectAR Notes layer | A note |
| Material, or the display color without one | The object's material (Rhino objects have one) |
| Point, spot, directional, rectangular light, sun | Point, spot, sun and area lights |
| Active view, named views | Cameras; the active view is the active camera |
| Document units | Converted: everything is sent in meters, with `unitScale` 1 |
| Document name | The scene name in the app and its Library |

Changes from the app are applied between Rhino commands, never in the middle of one, and each batch is one undo step.

---

## Differences from the Blender add-on

- **Moves.** Blender objects have a transform; Rhino objects don't. The add-on infers moves from Rhino's transform events, so a move sends a matrix only. Mirroring and non-uniform scaling send the mesh again.
- **One material per object.** Rhino objects have one material; per-face materials on polysurfaces aren't sent yet.
- **Texture mapping.** UVs come from the render mesh. Custom mappings from the Mapping panel (box, planar…) aren't applied yet.
- **Normal maps** aren't sent: Rhino's bump slot can hold a height map or a normal map, and the two can't be told apart reliably.
- **Light brightness.** A light's power in watts is sent when Rhino has one. Otherwise its intensity scales Blender's defaults (1,000 W for a point or spot light at full intensity, 200 W for a rectangular light, 3 W/m² for the sun), which is what the app reads.
- **No docked panel.** Rhino doesn't let a script add one, so ProjectAR is a floating window.
- **Snapshots** are written by the add-on itself, as USDZ with UsdPreviewSurface materials, instead of by Rhino's exporter. They pass Apple's `usdchecker --arkit`.

---

## First run in Rhino

Nothing here has run inside Rhino yet. A first session should go through:

1. Run `ProjectAR.py`. The window opens and "ProjectAR for Rhino 0.1.0 loaded" appears in the command history.
2. **Start Server**, connect from the app, **Allow**. A box and a SubD appear in AR at the right size in mm and in m, and a point light lights a millimeter model.
3. Move, rotate and scale an object with the Gumball and with Move. The AR view follows. Edit points or Boolean: the shape updates.
4. A block with a nested block, placed several times. Edit the block.
5. A physically based material with a color texture, a legacy material, and an object with only a layer color.
6. Variant set, note both ways, material try-on, room scan, decision with photo, Send Snapshot, camera from the phone.
7. A study session from start to end.

Errors appear at the top of the Connect tab and in the command history, with details in the Script Editor's output.

---

## Project layout

```
Rhino Add-on/
├── ProjectAR.py            # run this in Rhino: opens the window
├── build.py                # packages the add-on into dist/
├── README.md, CHANGELOG.md
├── projectar/
│   ├── app.py              # controller: server, timer, study, window
│   ├── ui.py               # the ProjectAR window (Eto)
│   ├── rhino_host.py       # reads the document: objects, blocks, materials, lights, views, events
│   ├── rhino_edit.py       # changes from the app and the window: materials, variants, notes, scans, camera
│   ├── streaming.py        # what each device is missing, sent as frames (no Rhino)
│   ├── protocol.py         # frames, meshes, matrices, axis conversion (no Rhino)
│   ├── server.py           # threaded TCP server, same as Blender's
│   ├── discovery.py        # Bonjour advertising
│   ├── snapshot.py         # USDZ writer and snapshot chunks
│   ├── textures.py         # texture ids and kind 3 frames
│   ├── decisions.py        # decision records, log and CSV, same as Blender's
│   ├── study.py            # study sessions, same as Blender's
│   ├── ids.py              # readable object ids
│   ├── settings.py         # saved settings
│   └── wheels/             # zeroconf, ifaddr, async_timeout (pure Python)
└── tests/                  # python3 -m unittest discover -s tests
```

Only `app.py`, `ui.py`, `rhino_host.py` and `rhino_edit.py` use Rhino. The rest is plain Python, and much of it matches the Blender add-on line for line (server, study, decisions). The wire protocol is specified in the Blender add-on's [specification](../Blender%20Add-on/ProjectAR-Blender-Addon.md#4-wire-protocol-version-1) and the app's developer guide, section 4.8.

### Tests

```bash
cd "Rhino Add-on" && python3 -m unittest discover -s tests
```

- `test_streaming.py` runs the streamer against a real socket and a fake app that decodes every frame the way the app does. It covers approval, moves, edits, materials, blocks for new and old apps, pausing, textures, snapshots and document switches.
- `test_rhino_fake.py` runs the Rhino side, and the window, against `fake_rhino.py`, a stand-in for RhinoCommon and Eto.
- `test_pure.py` checks study orders and CSV columns against the Blender add-on's code. It also checks decisions, ids and settings, and that snapshot files are valid USDZ.

---

## License

GPL-3.0-or-later, like the Blender add-on. The ProjectAR iOS app is a separate program and is not covered by this license.
