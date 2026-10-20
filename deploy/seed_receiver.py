"""One-time move of the existing data to a fresh server volume (Railway, Render).

deploy/start.sh runs this instead of the service when the volume has no jobbot.db yet and JOBBOT_SEED_TOKEN is
set. It listens on $PORT for one PUT /seed carrying a .tar.gz made by tools/push_seed.py (jobbot.db, the owner's
.env, the key file, each account's home), with the header X-Seed-Token. It checks the database, unpacks it into
JOBBOT_DATA and exits, after which start.sh starts the real service. Nothing else is answered.
Standard library only, so it runs before anything else is set up."""
import hmac
import io
import os
import sqlite3
import sys
import tarfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DATA = Path(os.environ.get("JOBBOT_DATA", "/data"))
TOKEN = os.environ.get("JOBBOT_SEED_TOKEN", "")
LIMIT = 2 * 1024 ** 3


class Seed(BaseHTTPRequestHandler):
    def _reply(self, code, text):
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Railway's health check and anyone curious: say what this is, nothing more.
        self._reply(200 if self.path == "/health" else 503, "waiting for the one-time data upload")

    def do_PUT(self):
        if self.path != "/seed" or not TOKEN or not hmac.compare_digest(self.headers.get("X-Seed-Token", ""), TOKEN):
            return self._reply(403, "no")
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= LIMIT:
            return self._reply(413, "bad size")
        blob = io.BytesIO()
        left = size
        while left:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                return self._reply(400, "upload cut short")
            blob.write(chunk)
            left -= len(chunk)
        blob.seek(0)
        stage = DATA / ".seed-incoming"
        try:
            with tarfile.open(fileobj=blob, mode="r:gz") as tar:
                tar.extractall(stage, filter="data")   # refuses absolute paths, .. and links out of the folder
            con = sqlite3.connect(stage / "jobbot.db")
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            if ok != "ok":
                return self._reply(422, f"database check failed: {ok}")
            for item in stage.iterdir():
                item.replace(DATA / item.name)
            stage.rmdir()
        except Exception as e:  # noqa: BLE001 - report anything back to the uploader
            return self._reply(400, f"could not unpack: {e}")
        self._reply(200, "data in place; the service starts now")
        self.server.done = True


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8765"))), Seed)
    server.done = False
    print(f"waiting for PUT /seed into {DATA}", flush=True)
    while not server.done:
        server.handle_request()
    sys.exit(0)
