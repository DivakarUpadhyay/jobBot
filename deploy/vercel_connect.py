"""Point the two Vercel sites at the bot's server and redeploy them.

The pages (apps/customer-portal, apps/admin-panel) are hosted on Vercel; everything else (API, database, the
bot itself, its scheduler) runs on the server from deploy/ (Oracle VM). Vercel passes every request that is not
a page on to the server, so the pages and the API share one address and nothing in the pages changes.

Run on the PC, from the jobbot folder, once the server is up:
    python deploy/vercel_connect.py --host 1-2-3-4.sslip.io --api-key KEY
--host is DOMAIN from the server's deploy/.env (the admin site is admin.<host>). Make KEY on the server with
    docker compose -f deploy/docker-compose.yml exec admin python -m jobbot user key you@example.com --name vercel-cron
Without --api-key the pages are connected but the Vercel cron job cannot start runs (it answers 500).
Afterwards set JOBBOT_PUBLIC_URL=https://jobbot-portal.vercel.app in the server's deploy/.env and restart it,
so payment and mailbox sign-in returns land on the Vercel address.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

APPS = Path(__file__).resolve().parent.parent / "apps"
SCOPE = "divakars-projects-477fee2a"
PORTAL = "https://jobbot-portal.vercel.app/"


def point(vercel_json, server, redirect_app=None):
    data = json.loads(vercel_json.read_text(encoding="utf-8"))
    for rule in data["rewrites"]:
        if rule["destination"].startswith("https://"):
            rule["destination"] = re.sub(r"^https://[^/]+", server.rstrip("/"), rule["destination"])
    for rule in data.get("redirects", []):
        if rule["source"] == "/app" and redirect_app:
            rule["destination"] = redirect_app
    vercel_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def vercel(folder, *args, stdin=None):
    cmd = ["npx", "-y", "vercel@latest", *args, "--scope", SCOPE]
    done = subprocess.run(cmd, cwd=folder, input=stdin, text=True, capture_output=True, shell=True)
    if done.returncode:
        raise SystemExit(f"vercel {' '.join(args)} failed in {folder.name}:\n{done.stdout}\n{done.stderr}")
    return done.stdout


def set_env(folder, name, value):
    subprocess.run(["npx", "-y", "vercel@latest", "env", "rm", name, "production", "--yes", "--scope", SCOPE],
                   cwd=folder, capture_output=True, shell=True)   # absent the first time; that is fine
    vercel(folder, "env", "add", name, "production", stdin=value)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="the server's DOMAIN, e.g. 1-2-3-4.sslip.io")
    ap.add_argument("--api-key", help="an API key of the owner, for the Vercel cron job")
    a = ap.parse_args()
    host = re.sub(r"^https?://", "", a.host).strip("/")
    customer, admin = APPS / "customer-portal", APPS / "admin-panel"
    point(customer / "vercel.json", f"https://{host}")
    point(admin / "vercel.json", f"https://admin.{host}", redirect_app=PORTAL)
    set_env(admin, "JOBBOT_BACKEND_URL", f"https://admin.{host}")
    if a.api_key:
        set_env(admin, "JOBBOT_API_KEY", a.api_key)
    for folder in (customer, admin):
        print(folder.name, "->", re.findall(r"https://\S+\.vercel\.app", vercel(folder, "deploy", "--prod", "--yes"))[-1:])
    print("Done. Commit the two vercel.json files so the next deploy keeps the address.")


if __name__ == "__main__":
    main()
