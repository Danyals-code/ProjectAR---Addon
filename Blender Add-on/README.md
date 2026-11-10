<div align="center">

# ProjectAR for Blender

**Live-preview your Blender scene in augmented reality on iPhone and iPad.**

[![Version](https://img.shields.io/badge/version-1.4.0-E87D0D?style=flat-square&logo=blender&logoColor=white)](CHANGELOG.md)
[![Blender](https://img.shields.io/badge/Blender-4.2%2B_%C2%B7_tested_on_5.2_LTS-265787?style=flat-square&logo=blender&logoColor=white)](#requirements)
[![Platforms](https://img.shields.io/badge/platforms-Windows_%7C_macOS_%7C_Linux-555555?style=flat-square)](#requirements)
[![Protocol](https://img.shields.io/badge/protocol-v1-4C6EF5?style=flat-square)](ProjectAR-Blender-Addon.md#4-wire-protocol-version-1)
[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-2F855A?style=flat-square&logo=gnu&logoColor=white)](#license)

[Features](#features) · [Install](#installation) · [Usage](#usage) · [Panels](#panel-reference) · [Troubleshooting](#troubleshooting) · [Specification](ProjectAR-Blender-Addon.md) · [Changelog](CHANGELOG.md)

</div>

---

The add-on runs a small server inside Blender and streams the open scene over your local network to the **ProjectAR** iOS app. Move an object, add a modifier or tweak a material, and the AR view updates within a fraction of a second.

← Back to the [repository overview](../README.md) · Using Rhino? See [ProjectAR for Rhino](../Rhino%20Add-on/README.md).

## Features

### Streaming
- **Live updates.** Transforms are sent up to 60 times a second. Meshes are resent when their geometry changes, at most 10 times a second per object.
- **Only changes go over the network.** The whole scene is sent only when a device connects, when you open another .blend file, change the Unit Scale or switch scenes, or when you press **Resync**.
- **Modifiers applied.** What you see in the viewport is what you get in AR.
- **Instances.** Collection, particle and Geometry Nodes instances are streamed, up to a limit you set. Each unique mesh is sent once, so a 2,000-pebble scatter costs one pebble's worth of mesh data. Older apps get every instance as its own mesh.
- **Any Unit Scale.** Everything goes to the app in meters, so a scene modeled in millimeters lands at real size, and its lights reach as far and shine as brightly as in Blender's renders.
- **You choose what to stream:** everything visible, only the selection, or one collection. **Show in AR** keeps any object out.
- **AR origin.** The world origin or the 3D cursor becomes the point that lands where you tap.
- **Pause updates** during heavy edits. Devices catch up when you resume.
- **Protects slower phones** with per-object triangle and instance budgets, and resyncs devices that fall behind.

### Materials, lights and cameras
- **Materials and textures.** Base Color, Metallic, Roughness and Alpha come from the Principled BSDF that feeds the Material Output, through reroutes, node groups and Mix or Add Shaders. Image textures are sent on those inputs and on Normal.
- **Painted textures** are resent once you stop painting for a moment.
- **Changes Blender doesn't report** still arrive, because every streamed material is checked once a second. This covers undo, drivers and some node group edits.
- **Lights and cameras.** Point, spot, sun and area lights and cameras are sent, so the app can light the model like your scene and show where your cameras are.
- **Camera from the phone.** The app can move a camera to the phone's pose, so you frame a render by walking around the model.
- **USDZ snapshots.** **Send Snapshot** exports what's streamed as USDZ, with full materials and textures, to the app's Library.

### Review and research
- **Design variants.** Each child collection of an AR Variant Set is a design option. Switch options in the app or in the panel, and the other side follows.
- **Notes from AR** arrive as empties in a *ProjectAR Notes* collection. Notes you add or edit in Blender update every device.
- **Material try-on.** Materials picked in the app with *Also Apply in Blender* are assigned here. Looks made in the app become Principled BSDF materials.
- **Room scans** from a LiDAR iPhone or iPad arrive as named boxes in a *Room Scan* collection, placed around your model.
- **Decisions** from the room arrive with their photo, options, criteria and reason. **Record Decision** records your own, and both export to CSV.
- **Study sessions** for comparing Blender alone with AR use the same tasks, counterbalanced orders and CSV columns as the app's Study Mode, and log what the participant does in Blender.

**Streamed:** mesh, curve, surface, metaball and text objects, and instances of them, that are visible in the current view layer, with per-corner normals and the active UV map. Lights and cameras are sent as descriptions, not geometry.

## Requirements

| Item | Requirement |
|---|---|
| **Blender** | 4.2 or newer, tested on 5.2 LTS |
| **OS** | Windows, macOS or Linux |
| **Device** | The ProjectAR app on an ARKit-capable iPhone or iPad |
| **App version** | Variants, notes, try-on and room scans need app 1.2 or later. App materials, instancing, material updates, lights, cameras and snapshots need app 1.4 or later. Older apps simply don't use them. |
| **Network** | Computer and phone on the **same** Wi-Fi or LAN, without client isolation |

No Python packages need installing. The Bonjour libraries are bundled as wheels, and Blender installs them for you.

## Installation

1. Take `dist/projectar-<version>.zip` from this folder, or [build it](#building-the-zip).
2. In Blender, open **Edit › Preferences › Get Extensions**.
3. Open the **⌄** menu at the top right and choose **Install from Disk…**.
4. Select the zip. ProjectAR is enabled automatically.

### Building the zip

From the repository root, with Python 3:

```bash
python3 "Blender Add-on/build.py"
```

This writes `Blender Add-on/dist/projectar-<version>.zip` with the version from `blender_manifest.toml`, and deletes older zips.

To validate the manifest with Blender's own tooling (Blender must be on your `PATH`):

```bash
blender --command extension validate "./Blender Add-on/viewar"
```

```bash
blender --command extension build --source-dir "./Blender Add-on/viewar" --output-dir "./Blender Add-on/dist"
```

> [!NOTE]
> On macOS the Blender binary is at `/Applications/Blender.app/Contents/MacOS/Blender`.

## Usage

1. In the 3D Viewport, press **N** and open the **ProjectAR** tab.
2. Press **Start Server**. The panel shows this computer's address and port, and whether nearby devices can see it. The copy button puts the address on the clipboard, and Universal Clipboard can paste it on your iPhone.
3. In the ProjectAR app, pick your computer under **Nearby**, or enter the address.
4. Press **Allow** in Blender when the device asks (first time only).
5. Tap a surface in the app to place the scene. You see Blender's **Front** view first.
6. Work as usual.

| Button | What it does |
|---|---|
| **Pause Updates / Resume Updates** | Freezes what devices show, then sends everything that changed in the meantime |
| **Resync** | Sends the whole scene again |
| **Stop** | Disconnects every device and closes the port |
| **×** next to a device | Disconnects that device. The **Devices** box also shows how much data each device has received. |

### Allowing devices

A device that hasn't been allowed gets nothing from the scene, not even its name, and can't change anything.

- **First connection:** a popup appears, and **Allow** and **Deny** show at the top of the panel.
- **Allowed devices** are remembered and connect straight away. **Settings › Allowed Devices** lists them, and **×** forgets one.
- **Denied devices** can ask again after a minute.

> [!WARNING]
> Turn off **Ask Before Devices Connect** only on a network you trust completely.

## Panel reference

### Streaming

| Option | What it does |
|---|---|
| **Visible Objects / Selected Objects / Collection** | What to send. *Selected* is handy for checking one asset. *Collection* streams one collection you pick. |
| **AR Origin** | *World Origin*, or *3D Cursor* so a model far from the origin still lands where you tap. Use **Shift+S › Cursor to Selected** first. |
| **Include Instances** | Streams collection, particle and Geometry Nodes instances, up to **Max Instances** |
| **Stream Lights and Cameras** | Sends lights and cameras to the app |
| **Send Snapshot** | Sends what's streamed as a USDZ file to every connected device that can save one. Shown once a device is connected. |
| **Show *object* in AR** | Keeps the active object out of AR while it stays visible in Blender, for reference planes or helper meshes. **Alt+click** changes every selected object. Saved with the .blend file. |

These options last for the session and aren't saved in the .blend file, except **Show in AR**.

### Variants

1. Put each design option in its own collection inside one parent collection, for example *Sofa* › *Option A*, *Option B*.
2. Select the parent in the Outliner and press **Use Active Collection as Variant Set**.
3. Click an option to show it and hide the others, or switch in the app. The visible option is highlighted.

Hidden options are still streamed, so switching in the app is instant. **×** turns a set back into ordinary collections. Variant sets are saved with the .blend file.

### Notes

The panel lists every note with its author.
- **Select** a note's empty with the arrow.
- **Edit** the selected note's text in place.
- **Delete** a note with **×**.
- **Add Note at 3D Cursor** leaves a note from Blender.

Notes live in the *ProjectAR Notes* collection and are never streamed as geometry.

### Decisions

**Record Decision** takes an image of the 3D view, then asks:
- **What was decided:** a variant set, with its options listed and the visible one picked, or your own comma-separated options
- **The option chosen**
- **What mattered:** the app's seven criteria, plus your own
- **The reason**
- **How sure you are,** from 1 to 5

If something required is missing, your answers are kept for the next try. A chosen variant is shown afterwards.

Each decision in the list shows where it was made (room or Blender), with buttons to show its photo, show the chosen variant again, or delete it. **Open Log** shows the *ProjectAR Decisions* text. **Export CSV** uses the app's columns plus a `source` column (`app` or `blender`), with the photos in a folder beside the file.

### Study

For the screen condition of a study, where participants work in Blender instead of in the room.

1. Type the **Tasks** once, separated by commas (for example *Oak, Walnut, Glass*), and leave **Counterbalance Order** on. The orders are the same balanced Latin square as the app's, so participant P07 gets the same order in both conditions.
2. Enter the **Participant ID**. The number at its end picks the order, shown below it. Set **Order** to use another one.
3. Confirm **Participant Agreed to Take Part** and press **Start Session**.
4. Press **Start** and **End** for each task. **Add Marker** notes a moment. **End Session** ends any task being timed and writes the files.

**What's logged:**
- **Edits,** once per burst: moving or editing objects, changing materials, navigating the 3D view
- **Review actions:** variant switches, decisions and notes
- **Housekeeping:** undo, redo and saving
- **Changes made from the app**

**Where it's saved:** each session is a folder in the study folder (*Documents/ProjectAR Study* unless changed in Settings), with `events.csv` and `task-times.csv`. The study folder also gets `ProjectAR Task Times.csv` covering every session. Events are written as they happen, so a session cut short by a crash carries on when Blender opens again.

### Statistics

Shows the number of streamed objects, instances and triangles. It warns when the scene is heavy for older iPhones or the instance limit is reached, and lists objects skipped for exceeding the triangle budget.

### Settings

Also under **Edit › Preferences › Add-ons › ProjectAR**, and kept between sessions.

| Setting | Default | Notes |
|---|---|---|
| **Port** | `51515` | Can only be changed while the server is stopped |
| **Updates per Second** | `30` | Lower it to 15–20 for heavy animated scenes |
| **Max Triangles per Object** | `300,000` | Objects above this are skipped. Applies immediately. |
| **Max Instances** | `2000` | Instances beyond this aren't sent |
| **Ask Before Devices Connect** | on | See [Allowing devices](#allowing-devices) |
| **Start Server with Blender** | off | Starts streaming whenever Blender opens |

## Tips for older iPhones

- Keep the whole scene under about **1 million triangles**. The panel warns you above that.
- Lower the **viewport** level of Subdivision Surface modifiers while previewing.
- Hide objects you don't need, or untick **Show in AR**. They leave the stream immediately.
- Stream only **Selected Objects** or one **Collection** when working on part of a big scene.
- For heavy animated scenes, set **Updates per Second** to 15–20. Use **Pause Updates** while sculpting.

## Network and firewall setup

| Platform | What to do |
|---|---|
| **Windows** | When Windows Defender Firewall asks, allow Blender on **Private networks**. If your Wi-Fi profile is *Public*, change it to *Private*. Apple Bonjour isn't needed. |
| **macOS** | Allow Blender to "find devices on your local network" when asked. Change it later in **System Settings › Privacy & Security › Local Network**. |
| **Linux** | With a firewall such as `ufw`, open **TCP 51515** and **UDP 5353** (mDNS). |
| **All** | Guest networks and many office or university networks block device-to-device traffic. A home router or a phone hotspot works reliably. |

The server starts only when you press **Start Server**, listens only on your local network, and never contacts the internet.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Could not open port" | Another program, or a second Blender, is using the port. Pick another port in **Settings** and enter it in the app. |
| "Use manual IP in the app" | Automatic discovery didn't start. Typing the address still works. The system console shows the error. |
| The app never finds the computer | Usually the firewall or network ([see above](#network-and-firewall-setup)). Try the address by hand first, to tell discovery problems from connection problems. If the panel lists several addresses, try each: VPNs and Ethernet docks add extra ones. |
| Connected, but nothing appears | Tap a surface to place the scene. Check that the objects are visible in the current view layer, that **Stream** isn't set to an empty selection or collection, and that updates aren't paused. Then press **Resync**. |
| The model is far from where I tapped | It's far from Blender's world origin. Set **AR Origin** to **3D Cursor** and snap the cursor to the model. |
| An object is missing | It's over the triangle budget (listed as *Skipped*), an unsupported type, has **Show in AR** off, or is an instance beyond **Max Instances**. |
| Wrong colors | Colors come from the Principled BSDF that feeds the Material Output. When Base Color comes from something other than an image texture or an RGB node, such as a procedural texture or a color ramp, the material's **Viewport Display** color is used. |
| The scene is huge or tiny | Everything is sent in meters, converted with **Scene Properties › Units › Unit Scale**. Check that it matches the units you modeled in: 1 for meters, 0.001 for millimeters. Changes reach devices automatically. |

The latest error shows at the top of the ProjectAR panel. Full tracebacks go to the system console. On Windows open it with **Window › Toggle System Console**. On macOS and Linux, start Blender from a terminal.

## Technical notes

### Source layout

```
Blender Add-on/
├── README.md, CHANGELOG.md
├── ProjectAR-Blender-Addon.md   # full specification and wire protocol
├── build.py                     # packages viewar/ into dist/
├── dist/                        # ready-to-install zip
├── viewar/
│   ├── blender_manifest.toml    # extension metadata, permissions, wheels
│   ├── __init__.py              # preferences, panels, operators, handlers, timer
│   ├── server.py                # threaded TCP server with length-prefixed frames
│   ├── discovery.py             # network addresses and Bonjour (_viewar._tcp)
│   ├── scene_stream.py          # change tracking, filtering, instances, meshes
│   ├── materials.py             # reading materials, making the app's looks
│   ├── textures.py              # image textures for apps that ask for them
│   ├── extras.py                # lights, cameras, camera from the device
│   ├── snapshot.py              # USDZ snapshots
│   ├── review.py                # variants, notes, room scans, decisions
│   ├── study.py                 # study sessions for the screen condition
│   └── wheels/                  # zeroconf, ifaddr, async_timeout (pure Python)
└── tests/                       # run inside Blender, see test_units.py
```

### How it works

```
depsgraph_update_post ──► SceneStreamer.on_depsgraph_update()   marks changed objects dirty
bpy.app.timers        ──► SceneStreamer.tick()                  removals, meshes, transforms, ping, full sync
                                    │
                                    ▼
                       Server ──► one sender thread per device
```

All `bpy` access happens on Blender's main thread. Only finished byte frames go to the network threads.

Instances come from `depsgraph.object_instances`. Apps that list `instancing` get one prototype mesh (`~mesh/<n>`) per unique geometry and a small `instances` entry for each instance. Older apps get each instance as an ordinary object with a stable id such as `Trees/Pine#0.12` (instancer / source object # persistent id). A mesh shared by many instances is triangulated once per update.

### Wire protocol v1, in short

| Frame kind | Payload |
|---|---|
| `1` JSON | `hello`, `clear`, `transforms`, `remove`, `ping`, `variants`, `notes`, `instances`, `objectMaterials`, `lights`, `cameras` and more |
| `2` Mesh | Little-endian positions, normals, optional UVs, triangle indices and per-triangle material indices, in RealityKit's Y-up space and in meters |
| `3` Texture | An image texture for a material |
| `4` Snapshot | One chunk of a USDZ file, at most 1 MB |

Each frame is a 4-byte big-endian length, a 1-byte kind, then the payload. The app can send `setMaterial`, `setVariant`, `addNote`, `removeNote`, `addRoomScan`, `addDecision`, `removeDecision`, `setCamera` and `requestSnapshot`, as announced in `hello.features`. Section 4 of the [specification](ProjectAR-Blender-Addon.md#4-wire-protocol-version-1) has the full details.

### Tests

```bash
blender -b --factory-startup --python-exit-code 1 --python "Blender Add-on/tests/test_units.py"
```

`tests/test_units.py` streams a scene to a fake app on `127.0.0.1` (the Rhino add-on's `tests/fake_app.py`). It checks that a scene in millimeters reaches the app exactly as the same scene in meters, and that changes from the app land in Blender units.

### Updating the discovery wheels

The bundled `zeroconf 0.39.4` is the newest release published as a pure Python (`py3-none-any`) wheel, so one file works on every OS. Newer releases only ship platform-specific wheels. To upgrade, download one wheel per platform and list them all in `blender_manifest.toml`:

```bash
pip download zeroconf ifaddr async-timeout --no-deps --only-binary=:all: --python-version 3.11 -d "Blender Add-on/viewar/wheels"
```

Without the wheels, the add-on still works, and users type the address by hand.

## License

GPL-3.0-or-later, because the add-on uses Blender's Python API. The ProjectAR iOS app is a separate program and isn't covered by this license.
