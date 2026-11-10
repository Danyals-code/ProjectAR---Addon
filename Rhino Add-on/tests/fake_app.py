# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for the ProjectAR app: connects, says what it can read, and
decodes every frame the way the app does (see the app's Protocol.swift), so
tests can check what a device ends up with."""

import json
import socket
import struct
import threading
import time


class FakeApp:
    def __init__(self, port, features=("textures", "materialUpdates", "instancing", "snapshot"),
                 device_id="TEST-DEVICE", name="Test iPhone", identify=True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.features = list(features)
        self.lock = threading.Lock()
        self.frames = []  # (kind, decoded)
        self.closed = False
        self.reset()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        if identify:
            self.send({"type": "client", "features": self.features, "deviceID": device_id, "deviceName": name})

    def reset(self):
        with self.lock:
            self.hello = None
            self.approvals = []
            self.meshes = {}  # id -> header + decoded arrays
            self.prototypes = {}
            self.instances = {}  # id -> prototype id
            self.sources = {}
            self.matrices = {}
            self.materials = {}  # id -> material infos
            self.extras = {}  # variants / notes / materials / lights / cameras
            self.textures = {}
            self.snapshots = {}
            self.failed = []
            self.removed = []
            self.counts = {}

    def send(self, message):
        payload = json.dumps(message).encode("utf-8")
        self.sock.sendall(struct.pack(">IB", len(payload), 1) + payload)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _read(self):
        buffer = bytearray()
        try:
            while True:
                chunk = self.sock.recv(1 << 16)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 5:
                    length, kind = struct.unpack(">IB", buffer[:5])
                    if len(buffer) < 5 + length:
                        break
                    payload = bytes(buffer[5:5 + length])
                    del buffer[:5 + length]
                    self._frame(kind, payload)
        except OSError:
            pass
        self.closed = True

    def _count(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1

    def _frame(self, kind, payload):
        with self.lock:
            if kind == 1:
                message = json.loads(payload.decode("utf-8"))
                self.frames.append((1, message))
                self._json(message)
            elif kind == 2:
                header_length = struct.unpack("<I", payload[:4])[0]
                header = json.loads(payload[4:4 + header_length].decode("utf-8"))
                body = payload[4 + header_length:]
                v, i = header["vertexCount"], header["indexCount"]
                expected = v * 12 * 2 + (v * 8 if header["hasUVs"] else 0) + i * 4 + (i // 3) * 4
                assert len(body) == expected, f"mesh {header['id']}: {len(body)} bytes, expected {expected}"
                positions = struct.unpack(f"<{v * 3}f", body[:v * 12])
                offset = v * 24 + (v * 8 if header["hasUVs"] else 0)
                indices = struct.unpack(f"<{i}I", body[offset:offset + i * 4])
                assert all(0 <= x < v for x in indices), "index out of range"
                entry = dict(header, positions=positions, indices=indices)
                self.frames.append((2, header))
                self._count("mesh")
                if header.get("prototype"):
                    self.prototypes[header["id"]] = entry
                else:
                    self.meshes[header["id"]] = entry
                self.materials[header["id"]] = header["materials"]
            elif kind == 3:
                header_length = struct.unpack("<I", payload[:4])[0]
                header = json.loads(payload[4:4 + header_length].decode("utf-8"))
                self.textures[header["id"]] = payload[4 + header_length:]
                self.frames.append((3, header))
            elif kind == 4:
                header_length = struct.unpack("<I", payload[:4])[0]
                header = json.loads(payload[4:4 + header_length].decode("utf-8"))
                body = payload[4 + header_length:]
                parts = self.snapshots.setdefault(header["id"], {"header": header, "chunks": {}})
                parts["chunks"][header["index"]] = body
                self.frames.append((4, header))

    def _json(self, m):
        t = m["type"]
        self._count(t)
        if t == "hello":
            self.hello = m
        elif t == "clear":
            self.meshes.clear()
            self.prototypes.clear()
            self.instances.clear()
            self.matrices.clear()
        elif t == "approval":
            self.approvals.append(m["state"])
        elif t == "transforms":
            for item in m["objects"]:
                assert len(item["m"]) == 16
                self.matrices[item["id"]] = item["m"]
        elif t == "remove":
            for oid in m["ids"]:
                self.removed.append(oid)
                self.meshes.pop(oid, None)
                self.prototypes.pop(oid, None)
                self.instances.pop(oid, None)
                self.matrices.pop(oid, None)
        elif t == "instances":
            for item in m["instances"]:
                self.instances[item["id"]] = item["mesh"]
                self.sources[item["id"]] = item.get("source")
        elif t == "objectMaterials":
            for item in m["objects"]:
                self.materials[item["id"]] = item["materials"]
        elif t == "snapshotFailed":
            self.failed.append(m)
        elif t in ("variants", "notes", "materials", "lights", "cameras"):
            field = {"variants": "sets", "notes": "notes", "materials": "materials", "lights": "lights", "cameras": "cameras"}[t]
            self.extras[t] = m[field]

    def snapshot_bytes(self, snapshot_id):
        parts = self.snapshots.get(snapshot_id)
        if not parts:
            return None
        header = parts["header"]
        if len(parts["chunks"]) != header["count"]:
            return None
        return b"".join(parts["chunks"][i] for i in range(header["count"]))

    def wait(self, condition, timeout=3.0, tick=None):
        """Waits until condition() holds, running tick() (the streamer) meanwhile."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if tick is not None:
                tick()
            with self.lock:
                if condition():
                    return True
            time.sleep(0.02)
        with self.lock:
            return condition()
