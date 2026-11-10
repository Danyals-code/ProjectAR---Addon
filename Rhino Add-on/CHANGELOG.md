# Changelog

All notable changes to ProjectAR for Rhino. The version is `VERSION` in `projectar/__init__.py`. The wire protocol is shared with the Blender add-on and stays at **v1**.

## 0.1.0 — 2026-11-11

First version, with the features of the Blender add-on 1.4. Written against the RhinoCommon API and tested with stand-ins for the app and for RhinoCommon; not yet run in Rhino.

### Added
- Streaming of breps, extrusions, surfaces, meshes and SubDs as their render meshes. Everything is sent in meters with `unitScale` 1, whatever the document's units, so the app's lights (which reach 20 units) and light brightness (which assumes meters) work in millimeter and inch models.
- Moves, rotations and uniform scales send only a new matrix, found from Rhino's transform events. Other edits send the mesh again, at most 10 times a second per object.
- Block instances, nested blocks included, as instances with one prototype mesh per block object for apps that use instancing, and one mesh per instance for older apps.
- Materials: physically based (base color, metallic, roughness, alpha), older Rhino materials (diffuse, transparency, shine), and the display color for objects without one. Image textures on base color, roughness and metallic, scaled to the texture size setting.
- Material-only updates (`objectMaterials`) for apps that read them.
- Streaming visible objects, the selection, or one layer; keeping objects out of AR; an AR origin at a picked point or the selection's bottom center; pausing.
- Bonjour discovery as "ProjectAR Rhino on *computer*", several devices, asking before a new device connects, and remembered devices.
- Variant sets from layers, notes as text dots, material try-on (looks from the app become materials, kept by their key), room scans as named boxes on a Room Scan layer, decisions kept in the document with their photos, and CSV export.
- Lights (point, spot, directional, rectangular, sun) and cameras (the active view and named views). A light's power in watts is sent when Rhino has one; otherwise its intensity scales Blender's defaults. The app can move the active view's camera to the phone.
- USDZ snapshots, written by the add-on with UsdPreviewSurface materials and textures, Y up, in meters per unit.
- Study sessions with the app's task orders and CSV files, logging edits, commands, view navigation, undo and saving.
- The ProjectAR window (Connect, Stream, Review, Study, Settings) and saved settings.
- Tests: the streamer with a fake app, the Rhino side with a fake RhinoCommon, and comparisons with the Blender add-on's study and decision code.
