# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal threaded TCP server with length-prefixed frames."""

import json
import queue
import socket
import struct
import sys
import threading
import time

KIND_JSON = 1
KIND_MESH = 2

# If a phone falls this far behind on updates, drop its backlog and resync it.
# A full sync itself doesn't count, however large the scene: otherwise a big
# scene on slow Wi-Fi would be dropped and resent forever.
MAX_PENDING_BYTES = 64 * 1024 * 1024
# More devices than this are turned away, so a busy network can't stall Blender.
MAX_CLIENTS = 8
# A send that makes no progress for this long means the device is gone. It
# applies per chunk, so a slow but steady device can take as long as it needs.
SEND_TIMEOUT = 15.0
SEND_CHUNK = 256 * 1024
# Phones that leave the network without closing are noticed after about
# KEEPALIVE_IDLE + KEEPALIVE_INTERVAL * KEEPALIVE_COUNT seconds.
KEEPALIVE_IDLE = 10
KEEPALIVE_INTERVAL = 3
KEEPALIVE_COUNT = 3
# The app sends small JSON messages, plus decisions with a photo of a few
# hundred KB. Anything bigger is a broken client.
MAX_INCOMING_BYTES = 4 * 1024 * 1024


def encode_frame(kind, payload):
    return struct.pack(">IB", len(payload), kind) + payload


class Client:
    def __init__(self, conn, address, inbox):
        self._conn = conn
        self.address = address
        self._inbox = inbox
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._forced_pending = 0  # bytes of a full sync still queued
        self.bytes_sent = 0
        self.alive = True
        self.needs_full_sync = True
        # What the app said it supports, from its "client" message.
        self.features = set()
        # Texture ids already sent to this device.
        self.sent_textures = set()
        # "new" until the streamer decides, then "pending" while Blender asks,
        # "allowed", or "closing" once refused or timed out.
        self.approval = "new"
        self.connected_at = time.monotonic()
        self.pending_since = None
        self.last_ping = 0.0
        # From the app's "client" message. Older apps send neither.
        self.device_id = None
        self.device_name = None
        threading.Thread(
            target=self._send_loop,
            name=f"ProjectAR client {address[0]}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._receive_loop,
            name=f"ProjectAR receive {address[0]}",
            daemon=True,
        ).start()

    @property
    def pending_bytes(self):
        with self._lock:
            return self._pending

    @property
    def label(self):
        return f"{self.address[0]}:{self.address[1]}"

    @property
    def device_key(self):
        """What Blender remembers an allowed device by: its id, or for apps
        that send none, its IP address."""
        return f"id:{self.device_id}" if self.device_id else f"ip:{self.address[0]}"

    @property
    def display_name(self):
        return self.device_name or self.address[0]

    def finish(self):
        """Closes the connection once everything queued has been sent."""
        self.approval = "closing"
        self._queue.put((None, False))

    def send(self, data, force=False):
        if not self.alive:
            return
        with self._lock:
            overloaded = not force and self._pending - self._forced_pending > MAX_PENDING_BYTES
            if not overloaded:
                self._pending += len(data)
                if force:
                    self._forced_pending += len(data)
        if overloaded:
            self._drain()
            self.needs_full_sync = True
            # Whatever was queued is gone, textures included.
            self.sent_textures.clear()
            return
        self._queue.put((data, force))

    def _drain(self):
        while True:
            try:
                data, forced = self._queue.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                self._pending -= len(data)
                if forced:
                    self._forced_pending -= len(data)

    def _send_loop(self):
        try:
            while self.alive:
                try:
                    data, forced = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:  # finish(): everything before it is sent
                    break
                view = memoryview(data)
                for start in range(0, len(view), SEND_CHUNK):
                    self._conn.sendall(view[start : start + SEND_CHUNK])
                with self._lock:
                    self._pending -= len(data)
                    if forced:
                        self._forced_pending -= len(data)
                    self.bytes_sent += len(data)
        except OSError:
            pass
        finally:
            self.close()

    def _receive_loop(self):
        """Reads messages from the app and queues them for the main thread.
        Only JSON frames with a "type" are accepted."""
        buffer = bytearray()
        try:
            while self.alive:
                try:
                    chunk = self._conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break  # the app closed the connection
                buffer += chunk  # bytearray: appending stays cheap for large messages
                while len(buffer) >= 5:
                    length, kind = struct.unpack(">IB", buffer[:5])
                    if length > MAX_INCOMING_BYTES:
                        return
                    if len(buffer) < 5 + length:
                        break
                    payload = bytes(buffer[5 : 5 + length])
                    del buffer[: 5 + length]
                    if kind != KIND_JSON:
                        continue
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except (ValueError, RecursionError):  # bad UTF-8 or JSON, or absurdly deep nesting
                        continue
                    if isinstance(message, dict) and isinstance(message.get("type"), str):
                        self._inbox.put((self, message))
        except OSError:
            pass
        finally:
            self.close()

    def close(self):
        self.alive = False
        try:
            self._conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._conn.close()
        except OSError:
            pass


class Server:
    def __init__(self, port):
        self.port = port
        self._clients = []
        self._lock = threading.Lock()
        # (client, message) pairs from the app, handled on Blender's main thread.
        self.inbox = queue.Queue()
        self._sock = None
        self._thread = None
        self._running = False

    def start(self):
        self._check_port_free()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # Windows: SO_REUSEADDR would let a second Blender share the port.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
            sock.listen(4)
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.2)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._accept_loop, name="ProjectAR accept", daemon=True
        )
        self._thread.start()

    def _check_port_free(self):
        """On macOS and Linux, SO_REUSEADDR lets the wildcard bind succeed while
        another program listens on 127.0.0.1 with the same port, and local
        connections would then go to that program. Binding loopback first
        finds that out."""
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            return  # Windows refuses the shared bind by itself
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.port))
        finally:
            probe.close()

    @staticmethod
    def _configure(conn):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(SEND_TIMEOUT)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # The idle option is TCP_KEEPALIVE on macOS and TCP_KEEPIDLE elsewhere.
        idle = getattr(socket, "TCP_KEEPIDLE", None) or (0x10 if sys.platform == "darwin" else None)
        for option, value in ((idle, KEEPALIVE_IDLE),
                              (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTERVAL),
                              (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_COUNT)):
            if option is not None:
                try:
                    conn.setsockopt(socket.IPPROTO_TCP, option, value)
                except OSError:
                    pass  # not every system has every option

    def _accept_loop(self):
        while self._running:
            try:
                conn, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                # A device that resets before this point makes these fail.
                # That's its problem, not the server's.
                self._configure(conn)
                with self._lock:
                    if not self._running:
                        conn.close()
                        break
                    self._clients = [c for c in self._clients if c.alive]
                    if len(self._clients) >= MAX_CLIENTS:
                        conn.close()
                        continue
                    self._clients.append(Client(conn, address, self.inbox))
            except OSError:
                try:
                    conn.close()
                except OSError:
                    pass

    def live_clients(self):
        with self._lock:
            self._clients = [c for c in self._clients if c.alive]
            return list(self._clients)

    def take_messages(self, limit=100):
        """Messages received since the last call, oldest first."""
        out = []
        while len(out) < limit:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                break
        return out

    def disconnect(self, label):
        for client in self.live_clients():
            if client.label == label:
                client.close()

    def stop(self):
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=0.3)
        with self._lock:
            for client in self._clients:
                client.close()
            self._clients.clear()
