"""Optional: say "Grocy changed" on NATS after a write, standard library only.

Off unless GROCY_MCP_NATS_URL is set. Then every stock write through this package
(api.post/put/delete, except the shopping list) publishes an empty message on
GROCY_MCP_NATS_SUBJECT (default "pantry.changed"), at most once a second, so whoever
caches the pantry can read it again at once. Login: GROCY_MCP_NATS_USER and
GROCY_MCP_NATS_PASSWORD_FILE, if the server wants one.

NATS's client protocol is plain text (INFO, CONNECT, PUB, PING/PONG), so this needs
nothing beyond the standard library. It never raises to the caller: a broken
connection is retried in the background, and a missed announcement only means the
reader's own cache expires as usual.
"""

import json
import os
import socket
import sys
import threading
import time

NAME = "grocy-mcp"


def _login(url, user, password_file):
    host, port = url.split("://", 1)[-1].rsplit(":", 1)
    connect = {"verbose": False, "pedantic": False, "name": NAME, "lang": "python", "version": "1"}
    if password_file:
        with open(password_file) as f:
            connect.update(user=user, **{"pass": f.read().strip()})
    s = socket.create_connection((host, int(port)), timeout=10)
    f = s.makefile("rwb")
    if not f.readline().startswith(b"INFO "):
        s.close()
        raise ConnectionError("not a NATS server")
    f.write(b"CONNECT " + json.dumps(connect).encode() + b"\r\nPING\r\n")
    f.flush()
    line = f.readline()
    if not line.startswith(b"PONG"):
        s.close()
        raise ConnectionError((line or b"closed").decode().strip())
    return s, f


class Announcer:
    """Publish an empty message on `subject` after `poke()`, at most once a second."""

    def __init__(self, url, subject, user="", password_file=""):
        self.url, self.subject, self.user, self.password_file = url, subject, user, password_file
        self._poked = threading.Event()
        if url and subject:
            threading.Thread(target=self._run, daemon=True).start()

    def poke(self):
        self._poked.set()

    def _run(self):
        conn = None
        while True:
            self._poked.wait()
            self._poked.clear()
            try:
                if conn is None:
                    conn = _login(self.url, self.user, self.password_file)
                s, f = conn
                f.write(f"PUB {self.subject} 0\r\n\r\n".encode())
                f.flush()
            except OSError as e:
                print(f"NATS: {e}", file=sys.stderr, flush=True)
                if conn:
                    conn[0].close()
                conn = None
                self._poked.set()   # try again in a moment
                time.sleep(5)
                continue
            time.sleep(1)


_announcer = None


def changed(path: str) -> None:
    """Called after a successful write to `path` on Grocy's API."""
    global _announcer
    url = os.environ.get("GROCY_MCP_NATS_URL", "")
    if not url or "shopping" in path.lower():
        return
    if _announcer is None:
        _announcer = Announcer(url, os.environ.get("GROCY_MCP_NATS_SUBJECT", "pantry.changed"),
                               os.environ.get("GROCY_MCP_NATS_USER", ""), os.environ.get("GROCY_MCP_NATS_PASSWORD_FILE", ""))
    _announcer.poke()
