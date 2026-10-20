"""Send this PC's data to a fresh Railway/Render volume, once (see deploy/README.md, "Railway").

    .venv\\Scripts\\python.exe tools\\push_seed.py https://<your-app>.up.railway.app <JOBBOT_SEED_TOKEN>
    .venv\\Scripts\\python.exe tools\\push_seed.py --file seed.tar.gz   (to copy to a server with scp instead)

Packs a consistent copy of jobbot.db (made with SQLite's backup, so the running bot may stay on), the owner's
.env, the key file and every account's home (without generated output and logs) into one .tar.gz and PUTs it
to /seed, which deploy/seed_receiver.py answers only while the volume is still empty. Stop the bot on this PC
afterwards: two copies of the database sending on their own would email the same people twice."""
import io
import sqlite3
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"output", "logs", "reports", "__pycache__"}


def pack():
    blob = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=blob, mode="w:gz", compresslevel=6) as tar:
        copy = Path(tmp) / "jobbot.db"
        src, dst = sqlite3.connect(APP / "jobbot.db"), sqlite3.connect(copy)
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        src.close(), dst.close()
        tar.add(copy, "jobbot.db")
        for name in (".env", ".secret.key"):
            if (APP / name).exists():
                tar.add(APP / name, name)
        for path in sorted((APP / "accounts").rglob("*")):
            rel = path.relative_to(APP)
            if path.is_file() and not SKIP_DIRS & set(rel.parts):
                tar.add(path, rel.as_posix())
    return blob.getvalue()


def main(url, token):
    body = pack()
    print(f"uploading {len(body) / 1e6:.0f} MB ...", flush=True)
    req = urllib.request.Request(url.rstrip("/") + "/seed", data=body, method="PUT",
                                 headers={"X-Seed-Token": token, "Content-Type": "application/gzip"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        print(r.status, r.read().decode().strip())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--file":
        Path(sys.argv[2]).write_bytes(pack())
        sys.exit(print(f"wrote {sys.argv[2]}"))
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
