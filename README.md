# ProjectAR – AR View for Blender (Add-on)

Live preview your Blender scene in augmented reality on iPhone and iPad.

The add-on runs a small server inside Blender and streams the open scene over your local network to the **ProjectAR** iOS app. Move an object, add a modifier or tweak a material, and the AR view updates within a fraction of a second.

> The full technical specification (architecture, wire protocol, source walkthrough) is in [ProjectAR-Blender-Addon.md](ProjectAR-Blender-Addon.md).

---

## Features

- **Live streaming.** Object transforms are sent at up to 60 updates per second. Meshes are re-sent when their geometry or materials change (at most 10 times per second per object).
- **Only changes go over the network.** A full scene is sent only when a device connects, when you open another .blend file, change the Unit Scale or switch scenes, or when you press **Resync**.
- **Modifiers applied.** What you see in the viewport (evaluated geometry) is what you get in AR.
- **Instances.** Collection instances, particle instances and Geometry Nodes instances (scattering, arrays of objects) are streamed, up to a limit you set.
- **Choose what to stream.** Everything visible, only the selection, or one collection, and any object can be kept out of AR with **Show in AR**.
- **AR origin.** Use the world origin or the 3D cursor as the point that lands where you tap, so models far from the origin are easy to place.
- **Pause updates** while you make heavy edits. Devices catch up when you resume.
- **Materials and textures.** Base Color, Metallic, Roughness and Alpha from the Principled BSDF, plus image textures on those inputs and on Normal (through a Normal Map node). Painted textures update on the device once you pause.
- **Automatic discovery.** Blender appears in the app under *Nearby* via Bonjour (mDNS), on every network interface. You can also enter or paste the IP manually.
- **Multiple devices.** Several phones can connect at once, a phone that joins late receives the whole scene, and you can see how much data each device received or disconnect it.
- **Remembers your settings.** Port, update rate and limits are kept in Blender's preferences, and the server can start automatically with Blender.
- **Protects slower phones.** Per-object triangle and instance budgets, plus automatic resync for devices that fall behind.
- **Design variants.** Mark a collection as an AR Variant Set and each child collection becomes a design option. Switch options in the app (instantly, since every option is streamed) or in the panel, and the other side follows.
- **Notes from AR.** Notes left on the model in the app arrive as empties in a *ProjectAR Notes* collection, with the text shown in the viewport. Edit or delete them here, or add your own at the 3D cursor, and every connected device updates.
- **Material try-on.** Materials picked in the app with *Also Apply in Blender* are assigned to the object's slot here.
- **Room scans.** Scan a room in the app (LiDAR iPhone or iPad) and send it here as named boxes (Wall 1, Door 1, Table 1…) in a *Room Scan* collection. With your scene placed at real size, the room lands exactly where it is around your model. Scan objects are kept out of AR.
- **Decisions.** Decisions made in the room with the app arrive with their photo, options, criteria and reason. Record your own at the screen with **Record Decision**, with an image of the 3D view. Both go in one log, marked by where they were made, and export to CSV for studies.
- **Study sessions at the screen.** For comparing Blender alone with AR: the same tasks, counterbalanced orders and task timer as the app's Study Mode, plus a log of what the participant does in Blender (editing objects and materials, switching variants, decisions, notes, navigating the 3D view, undo, saving). CSV columns match the app's exports.

**Streamed:** mesh, curve, surface, metaball and text objects (and instances of them) that are visible in the current view layer, with per-corner normals and the active UV map.
**Not yet streamed:** lights and cameras (see the [roadmap](#roadmap)).

The add-on still speaks wire protocol v1. Variants, notes, material try-on and room scans need ProjectAR app 1.2 or later; older apps simply don't use them.

---

## Requirements

| Item | Requirement |
|---|---|
| Blender | 4.2 or newer (tested on 5.2 LTS) |
| OS | Windows, macOS or Linux |
| iOS | The ProjectAR app on an ARKit-capable iPhone or iPad |
| Network | Computer and phone on the **same** Wi-Fi or LAN, without client isolation |

No extra Python packages need to be installed. The Bonjour libraries are bundled as wheels, and Blender installs them for you.

---

## Installation

### From the ready-made zip

1. Take `dist/projectar-<version>.zip` from this repository (or build it yourself, see below).
2. In Blender go to **Edit › Preferences › Get Extensions**.
3. Open the **⌄** dropdown in the top right and choose **Install from Disk…**.
4. Select the zip. ProjectAR is enabled automatically.

### Building the zip

The quickest way needs only Python 3:

```bash
python3 build.py
```

This writes `dist/projectar-<version>.zip`, taking the version from `blender_manifest.toml`, and deletes older zips.

To also validate the manifest, use Blender's own tooling (Blender must be on your `PATH`):

```bash
blender --command extension validate ./blender/viewar
```

```bash
blender --command extension build --source-dir ./blender/viewar --output-dir ./dist
```

On macOS the Blender binary is at `/Applications/Blender.app/Contents/MacOS/Blender`.

---

## Usage

1. In the 3D Viewport press **N** to open the sidebar, then select the **ProjectAR** tab.
2. Press **Start Server**. The panel shows this computer's address and port (plus any other network addresses), and whether it is visible to nearby devices. The copy button next to the address puts it on the clipboard. With Universal Clipboard you can paste it straight into the app on your iPhone.
3. Open the ProjectAR app and pick your computer under **Nearby**, or enter the address shown in the panel.
4. Tap a surface in the app to place the scene. You see Blender's **Front** view first.
5. Work as usual. **Pause Updates** freezes what the devices show, and **Resume Updates** sends everything that changed in the meantime.
6. **Resync** sends the whole scene again. **Stop** disconnects all devices and closes the port.

The **Devices** box lists each connected phone with the data it has received. The **×** button disconnects that phone.

### Allowing devices

The first time a device connects, a popup says it's asking, and the top of the ProjectAR panel shows Allow and Deny. Until you press **Allow**, it gets nothing from the scene, not even its name, and can't change anything. Allowed devices are remembered and connect straight away next time. They're listed under **Settings › Allowed Devices**, where **×** forgets one. **Deny** turns a device away. It can ask again after a minute. Turn off **Ask Before Devices Connect** in Settings only on a network you trust completely.

### Streaming sub-panel

| Option | What it does |
|---|---|
| **Visible Objects / Selected Objects / Collection** | What to send. *Selected* is handy for checking one asset; *Collection* streams a single collection you pick. |
| **AR Origin** | *World Origin*, or *3D Cursor* so a model far from the origin still lands where you tap (**Shift+S › Cursor to Selected** first). |
| **Include Instances** | Stream collection, particle and Geometry Nodes instances. Each instance is sent as its own mesh, so very large scatters are capped by **Max Instances**. |
| **Show *object* in AR** | Keeps the active object out of AR while it stays visible in Blender, for example reference planes or helper meshes. **Alt+click** changes all selected objects. This setting is saved with the .blend file. |

These options last for the session and are not saved into your .blend files.

### Variants sub-panel

1. Put each design option in its own collection, all inside one parent collection, for example *Sofa* › *Option A*, *Option B*.
2. Select the parent in the Outliner and press **Use Active Collection as Variant Set**.
3. The options are listed with the visible one highlighted. Click an option to show it and hide the others, or switch in the app.

Hidden options are still streamed, so switching in the app is instant. **×** turns a set back into ordinary collections. The setting is saved with the .blend file.

### Notes sub-panel

Lists every note with its author. Click the arrow to select a note's empty, edit the text of the selected note in place, or press **×** to delete it. **Add Note at 3D Cursor** leaves a note from Blender. Notes live in the *ProjectAR Notes* collection and are never streamed as geometry.

### Decisions sub-panel

**Record Decision** takes an image of the 3D view, then asks what you decided: a variant set (its options are listed, the visible one picked) or your own comma-separated options, the option chosen, what mattered (the same seven criteria the app suggests, plus your own), the reason, and how sure you are from 1 to 5. A chosen variant is shown afterwards. If something required is missing, your answers are kept for the next try.

Each decision is listed with its choice, the other options, the criteria, the reason, and whether it was made in the room or in Blender. Buttons show the photo, show the chosen variant again, or delete the decision. **Open Log** shows the *ProjectAR Decisions* text, and **Export CSV** saves every decision with the same columns as the app's export plus a `source` column (`app` or `blender`), with the photos in a folder beside the file.

### Study sub-panel

For the screen condition of a study, where participants work in Blender instead of in the room.

1. Type the **Tasks** once, separated by commas (for example *Oak, Walnut, Glass*), and leave **Counterbalance Order** on. The orders are the same balanced Latin square as the app's, so participant P07 gets the same order in both conditions.
2. Enter the **Participant ID**. The number at its end picks the order, shown below it. Set **Order** to use another one.
3. Confirm **Participant Agreed to Take Part** and press **Start Session**.
4. Press **Start** and **End** for each task. **Add Marker** notes a moment. **End Session** ends any task being timed and writes the files.

Edits are logged as they pause, once per burst: moving or editing an object, changing a material, navigating the 3D view. Variant switches, decisions, notes, undo, redo, saving and changes made from the app are logged too. Each session is a folder in the study folder (Documents/ProjectAR Study unless changed in Settings) with `events.csv` and `task-times.csv`, and the study folder gets `ProjectAR Task Times.csv` over every session. Events are written to disk as they happen, so a session left running when Blender closes or crashes carries on when it opens again.

### Statistics sub-panel

This shows the number of streamed objects, instances and triangles. It also warns when the scene is heavy for older iPhones, when the instance limit is reached, and which objects were skipped for exceeding the triangle budget.

### Settings sub-panel

These are also under **Edit › Preferences › Add-ons › ProjectAR**, and Blender remembers them between sessions:

| Setting | Default | Notes |
|---|---|---|
| **Port** | `51515` | Can only be changed while the server is stopped. |
| **Updates per Second** | `30` | Lower it to 15–20 for heavy animated scenes. |
| **Max Triangles per Object** | `300,000` | Objects above this are skipped. Changing it applies immediately. |
| **Max Instances** | `500` | Instances beyond this are not sent. |
| **Start Server with Blender** | off | Starts streaming automatically whenever Blender opens. |

### Tips for smooth results on older iPhones

- Keep the whole scene under about **1 million triangles**. The panel warns you above that.
- Lower the **viewport** level of Subdivision Surface modifiers while previewing.
- Hide objects you don't need, or untick **Show in AR**. They are removed from the stream immediately.
- Stream only the **Selected Objects** or one **Collection** when you are working on part of a big scene.
- For heavy animated scenes, reduce **Updates per Second** to 15–20, and use **Pause Updates** during heavy edits such as sculpting.

---

## Network and firewall setup

| Platform | What to do |
|---|---|
| **Windows** | When Windows Defender Firewall asks, allow Blender on **Private networks**. If your Wi-Fi profile is set to *Public*, change it to *Private*. You don't need to install Apple Bonjour. |
| **macOS** | Allow Blender to "find devices on your local network" when asked. You can change this later in **System Settings › Privacy & Security › Local Network**. |
| **Linux** | If you use a firewall (e.g. `ufw`), open **TCP 51515** and **UDP 5353** (mDNS). |
| **All** | Guest networks and many office or university networks block device-to-device traffic. A home router or phone hotspot works reliably. |

The server only starts when you press **Start Server**, listens only on your local network, and never contacts the internet.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Could not open port" | Another program or a second Blender is using the port. Choose a different port in **Settings** and enter it in the app. |
| "Use manual IP in the app" | Auto discovery failed to start. Manual IP still works. Check the system console for the error. |
| App never finds the computer | Usually the firewall or network (see above). Try manual IP first to tell discovery problems apart from connection problems. If the panel lists several addresses, try each; VPNs and docks with Ethernet add extra ones. |
| Connected, but nothing appears | Tap a surface to place the scene. Check that the objects are visible in the current view layer, that **Stream** is not limited to an empty selection or collection, and that updates are not paused. Then press **Resync**. |
| Model appears far away from where I tapped | It is far from Blender's world origin. Set **AR Origin** to **3D Cursor** and snap the cursor to the model. |
| An object is missing in AR | It is over the triangle budget (listed as *Skipped*), is an unsupported type, has **Show in AR** turned off, or is an instance beyond **Max Instances**. |
| Wrong colors | Only unlinked Principled BSDF values are read. If Base Color is driven by a texture, the material's viewport display color is used. |
| Scene is huge or tiny | Check **Scene Properties › Units › Unit Scale**. One Blender unit equals one meter at Unit Scale 1. Changes are sent to devices automatically. |

The most recent error is shown at the top of the ProjectAR panel. Full tracebacks are printed to the system console (**Window › Toggle System Console** on Windows, or start Blender from a terminal on macOS and Linux).

---

## Project layout

```
.
├── README.md
├── CHANGELOG.md
├── ProjectAR-Blender-Addon.md   # full specification and wire protocol
├── build.py                     # packages the add-on into dist/
├── dist/
│   └── projectar-<version>.zip  # ready to install
└── blender/
    └── viewar/
        ├── blender_manifest.toml   # extension metadata, permissions, wheels
        ├── __init__.py             # preferences, panels, operators, handlers, timer
        ├── server.py               # threaded TCP server and frame encoding
        ├── discovery.py            # network addresses and Bonjour (_viewar._tcp) advertising
        ├── scene_stream.py         # change tracking, filtering, instances and mesh export
        └── wheels/                 # zeroconf, ifaddr, async_timeout (pure Python)
```

### How it works

```
depsgraph_update_post ──► SceneStreamer.on_depsgraph_update()   mark changed objects dirty
bpy.app.timers        ──► SceneStreamer.tick()                  remove / meshes / transforms / ping / full sync
                                    │
                                    ▼
                       Server ──► one sender thread per device
```

All `bpy` access happens on Blender's main thread. Only finished byte frames are passed to the network threads.

Instances come from `depsgraph.object_instances`. Protocol v1 has no instancing, so each instance is sent as an ordinary object with a stable id such as `Trees/Pine#0.12` (instancer / source object # persistent id). A mesh shared by many instances is triangulated only once per update.

### Wire protocol (v1), in short

Each frame has a 4-byte big-endian length, a 1-byte kind (`1` = JSON, `2` = binary mesh), then the payload. JSON messages are `hello`, `clear`, `transforms`, `remove`, `ping`, `variants` and `notes`. Since 1.2 the app can also send `setMaterial`, `setVariant`, `addNote`, `removeNote` and `addRoomScan`, and since 1.3 `addDecision` and `removeDecision`, which the add-on announces in `hello.features`. Mesh payloads carry little-endian positions, normals, optional UVs, triangle indices and per-triangle material indices, already converted to RealityKit's Y-up space. Full details are in section 4 of the [specification](ProjectAR-Blender-Addon.md#4-wire-protocol-version-1).

### Updating the discovery wheels

The bundled `zeroconf 0.39.4` is the newest release published as a pure Python (`py3-none-any`) wheel, so one file works on every OS. Newer zeroconf releases only ship platform-specific wheels. If you upgrade, download one wheel per platform and list them all in `blender_manifest.toml`:

```bash
pip download zeroconf ifaddr async-timeout --no-deps --only-binary=:all: --python-version 3.11 -d blender/viewar/wheels
```

If you remove the wheels entirely, the add-on still works and users type the IP manually.

---

## Roadmap

These need a protocol update in the iOS app as well:

1. Image textures (base color, normal, roughness)
2. Native instancing (send each shared mesh once plus instance matrices, instead of one mesh per instance)
3. Pairing code for connections (more important now that the app can change the scene)
4. One-shot USDZ snapshot for full material fidelity
5. Delta meshes (send only positions when topology is unchanged)
6. Lights and cameras

---

## License

GPL-3.0-or-later, because the add-on uses Blender's Python API. The ProjectAR iOS app is a separate program and is not covered by this license.
