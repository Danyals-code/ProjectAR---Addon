# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal threaded TCP server with length-prefixed frames."""

import json
import queue
import socket
import struct
import threading

KIND_JSON = 1
KIND_MESH = 2

# If a phone falls this far behind, drop its backlog and resync it.
MAX_PENDING_BYTES = 64 * 1024 * 1024
SEND_TIMEOUT = 15.0
# The app only sends small JSON messages. Anything bigger is a broken client.
MAX_INCOMING_BYTES = 1024 * 1024


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
        self.bytes_sent = 0
        self.alive = True
        self.needs_full_sync = True
        threading.Thread(
            target=self._send_loop,
            name=f"ViewAR client {address[0]}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._receive_loop,
            name=f"ViewAR receive {address[0]}",
            daemon=True,
        ).start()

    @property
    def label(self):
        return f"{self.address[0]}:{self.address[1]}"

    def send(self, data, force=False):
        if not self.alive:
            return
        with self._lock:
            overloaded = not force and self._pending > MAX_PENDING_BYTES
            if not overloaded:
                self._pending += len(data)
        if overloaded:
            self._drain()
            self.needs_full_sync = True
            return
        self._queue.put(data)

    def _drain(self):
        while True:
            try:
                data = self._queue.get_nowait()
            except queue.Empty:
                return
            with self._lock:
                self._pending -= len(data)

    def _send_loop(self):
        try:
            while self.alive:
                try:
                    data = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._conn.sendall(data)
                with self._lock:
                    self._pending -= len(data)
                    self.bytes_sent += len(data)
        except OSError:
            pass
        finally:
            self.close()

    def _receive_loop(self):
        """Reads messages from the app and queues them for the main thread.
        Only JSON frames with a "type" are accepted."""
        buffer = b""
        try:
            while self.alive:
                try:
                    chunk = self._conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break  # the app closed the connection
                buffer += chunk
                while len(buffer) >= 5:
                    length, kind = struct.unpack(">IB", buffer[:5])
                    if length > MAX_INCOMING_BYTES:
                        return
                    if len(buffer) < 5 + length:
                        break
                    payload, buffer = buffer[5 : 5 + length], buffer[5 + length :]
                    if kind != KIND_JSON:
                        continue
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except ValueError:  # includes UnicodeDecodeError
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
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
            sock.listen(4)
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._accept_loop, name="ViewAR accept", daemon=True
        )
        self._thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                conn, address = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(SEND_TIMEOUT)
            with self._lock:
                if not self._running:
                    conn.close()
                    break
                self._clients.append(Client(conn, address, self.inbox))

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
            self._thread.join(timeout=1.0)
        with self._lock:
            for client in self._clients:
                client.close()
            self._clients.clear()
