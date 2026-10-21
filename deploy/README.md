# Putting JobPilot on a server

**Status:** the files are written and match the code as of 2026-10-18, but the image has **not yet been built on
a real server** (Docker is not installed on the PC they were written on). Expect small fixes the first time.
Everything else (sign-in, accounts, payments, backups, alerts, the API) is covered by `tests/run_all.py`
on Windows; the Linux-specific parts (file locks with `fcntl`, process groups) are written but untested.

## What you get

| Address | What | Process |
|---|---|---|
| `https://DOMAIN/` | Customer portal: sign-up, profile, plans, payments | `customer` (port 8765) |
| `https://ADMIN_DOMAIN/` | Admin panel, console, `/docs` | `admin` (port 8766) |
| inside `admin` | Scheduler (customers' runs), daily backup, alert checks | `admin` |

Only the HTTPS proxy (Caddy) is reachable from outside; it gets its certificates by itself. `/metrics` is not
served to the internet.

## What you need

| Thing | Notes |
|---|---|
| A Linux server (VPS) | 2 CPU, 4 GB memory to start (Chrome needs the memory). About US$20–40 a month |
| Two DNS names | `A` records for `DOMAIN` and `ADMIN_DOMAIN` pointing at the server |
| Docker with the compose plugin | On the server |
| Ports 80 and 443 open | For the proxy and its certificates |

## Steps

1. Copy the `jobbot` folder to the server (or `git clone` it). Leave out `.env`, `*.db`, `output`, `reports`,
   `logs`, `accounts`, `backup`, `LOGINS.local.txt` and `.secret.key`: they are your own data.
2. `cp deploy/env.example deploy/.env` and fill it in. At least: `DOMAIN`, `ADMIN_DOMAIN`, `JOBBOT_PUBLIC_URL`,
   `JOBBOT_SECRET_KEY` (keep a copy of it somewhere safe), the AI key, `JOBBOT_ALERT_EMAIL`, and the company
   details shown in the Terms and Privacy pages.
3. Start:
   ```
   docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
   ```
4. Create the operator (on the server itself; the first-user form is refused over the network on purpose):
   ```
   docker compose -f deploy/docker-compose.yml exec admin python -m jobbot user add you@example.com
   ```
5. Open `https://ADMIN_DOMAIN/` and sign in. System tab: health, alerts, backups, do-not-contact list.
6. Payments: create the Razorpay and/or Stripe keys, register the webhooks
   (`JOBBOT_PUBLIC_URL/webhooks/razorpay`, `JOBBOT_PUBLIC_URL/webhooks/stripe`), put the keys in `deploy/.env`,
   restart, then switch the gateway on under Payments in the admin panel and the test gateway off.
7. Mailbox sign-in: create a Google Cloud OAuth client and/or a Microsoft Entra app with the redirect address
   `JOBBOT_PUBLIC_URL/oauth/callback`, put the ids and secrets in `deploy/.env`, restart. Google allows only
   test users until the app passes its verification for the restricted Gmail scope.

## Where the data is

Everything that must be kept is in the Docker volume `data` (mounted at `/data`):

| Path | What |
|---|---|
| `/data/jobbot.db` | The one database: accounts, users, plans, payments, job posts, every account's applications |
| `/data/accounts/<name>/` | Each customer's files (generated resumes, uploads) and their mailbox file (encrypted) |
| `/data/backup/auto/` | Daily checked, compressed backups (7 daily + 4 weekly) |
| `/data/logs/service.jsonl` | The service log, one JSON object per line |

A second copy of every backup goes to the `backups` volume. Copy that volume off the server regularly
(for example with `rclone` to object storage); a backup on the same disk does not survive the disk.

## Backups and restore

```
docker compose -f deploy/docker-compose.yml exec admin python -m jobbot backup now      # make one now
docker compose -f deploy/docker-compose.yml exec admin python -m jobbot backup drill    # prove a restore works
docker compose -f deploy/docker-compose.yml stop customer admin
docker compose -f deploy/docker-compose.yml run --rm admin python -m jobbot backup restore /data/backup/auto/<file>.db.gz --yes
docker compose -f deploy/docker-compose.yml start customer admin
```

## Monitoring

- `https://ADMIN_DOMAIN/health/deep` answers 503 when the database, the scheduler or the disk has a problem:
  point an uptime monitor at it.
- `/metrics` (Prometheus format) answers inside the network with `Authorization: Bearer JOBBOT_METRICS_TOKEN`.
- Alerts (failed runs, a mailbox paused for bounces, a stale backup, low disk) are emailed to `JOBBOT_ALERT_EMAIL`
  and listed in the admin panel.

## Before you publish the image anywhere

`config.yaml` is the master list of settings and still carries your own sending addresses as defaults.
New accounts do not get them (tested), but they are in the file. Blank `sending.send_from` and `sending.cc`
in the copy you put on the server.

## Limits of this set-up

- One server, SQLite. Fine for a pilot and the first few hundred customers; beyond that, move to PostgreSQL
  (not built) and run more than one customer process.
- Mailbox sign-in keeps its short-lived sign-in state in the `customer` process's memory: run one `customer`
  process, or the callback may land on one that did not start the sign-in.

## Railway (one service, no server to look after)

`railway.json` in the repo root tells Railway to build `deploy/Dockerfile` and start `deploy/start.sh`: one
process serving the customer portal (`/app`), the admin panel (`/admin`) and the scheduler. Schedules and
settings live in the database, so after setup everything is done from the admin panel and runs happen on
time without this PC.

1. railway.com → New Project → Deploy from GitHub repo → `DivakarUpadhyay/jobBot`. Hobby plan (US$5 a month
   plus usage) is enough for a pilot.
2. On the service: **Volumes → Add volume**, mount path `/data`.
3. **Variables**:
   - `JOBBOT_DATA=/data`
   - `RAILWAY_RUN_UID=0` (the volume is owned by root; the image's user cannot write to it otherwise)
   - `JOBBOT_TRUSTED_PROXY=*`
   - `JOBBOT_SEED_TOKEN=` a long random string (only needed for step 5)
   - `JOBBOT_PUBLIC_URL=https://<your domain>`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, and the rest of
     `env.example` as you get them. Do not set `JOBBOT_SECRET_KEY` unless you also had it on the PC.
4. **Settings → Networking → Generate domain** (or add your own domain). Deploy. The log says
   `waiting for PUT /seed`.
5. Move the data from the PC, once (database, the owner's mail passwords, account folders):
   `.venv\Scripts\python.exe tools\push_seed.py https://<domain> <JOBBOT_SEED_TOKEN>`
   The service then starts by itself. Afterwards delete `JOBBOT_SEED_TOKEN` from Variables.
6. **Stop the bot on the PC** (`jobbot loop stop`, disable the "JobBot Loop" / "JobBot Daily 0830" scheduled
   tasks, close the admin/customer windows). Two databases sending on their own would email people twice.
7. Sign in at `https://<domain>/admin` with your existing admin login. Change settings, schedules and the bot
   switch there.

Every push to `main` on GitHub redeploys; the volume (database, backups) is kept. Without a seed, a fresh
volume starts empty and the first admin sign-in is created with `railway ssh` then
`python -m jobbot user add you@example.com --generate`.

Job sites may block requests from cloud addresses more often than from a home connection (104 in particular);
compare the first days' finds with the PC's.

## Free: Oracle Cloud Always Free

No monthly cost. Oracle asks for a card to verify identity; Always Free resources are not charged.

1. Sign up at oracle.com/cloud/free. Pick a home region near you (it cannot be changed later).
2. Compute → Instances → Create: image **Ubuntu 24.04**, shape **VM.Standard.A1.Flex** (Ampere ARM),
   2 OCPU / 12 GB is plenty (free up to 4 / 24). Download the SSH private key it offers. If it says
   "out of capacity", try another availability domain or again later.
3. Networking → the instance's VCN → Security List → Add ingress rules: source `0.0.0.0/0`, TCP, ports
   `80` and `443`.
4. On the PC: `.venv\Scripts\python.exe tools\push_seed.py --file seed.tar.gz`, then
   `scp -i <key> seed.tar.gz ubuntu@<public IP>:~/`
5. `ssh -i <key> ubuntu@<public IP>` and run
   `curl -fsSL https://raw.githubusercontent.com/DivakarUpadhyay/jobBot/main/deploy/oracle-setup.sh | bash`
   It prints the two addresses (`https://<ip>.sslip.io` customer portal, `https://admin.<ip>.sslip.io` admin
   panel; free HTTPS, no domain needed). Put `GEMINI_API_KEY` in `~/jobBot/deploy/.env`, then
   `cd ~/jobBot && sudo docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d`.
6. Stop the bot on the PC (see step 6 of the Railway section).

To update after a push to GitHub: run the same curl line again. The data volume is kept.

## Pages on Vercel (added 2026-10-21)

Vercel can host the pages but not the bot: it keeps no files between requests (the SQLite database would lose
every write), free runs stop after 5 minutes, and free cron jobs run once a day. So the pages are on Vercel and
the server above does everything else. Vercel passes every request that is not a page on to the server, so the
pages and the API share one address.

| Vercel project | Address | Folder | Passes the API to |
|---|---|---|---|
| `jobbot-portal` | https://jobbot-portal.vercel.app | `apps/customer-portal` | `https://DOMAIN` |
| `jobbot-admin` | https://jobbot-admin.vercel.app | `apps/admin-panel` | `https://ADMIN_DOMAIN` |

Scheduled job: `apps/admin-panel/api/cron.js` runs daily at 00:30 UTC (08:30 Taiwan; on the free plan Vercel
may start it at any time within that hour). It asks the server to start a run for the owner's account
(`POST /cycles`); the server skips it when a run is already going. Customers' own schedules (every 2 hours
and so on) run from the scheduler on the server, which is set from the Schedule tab in the admin panel.

Once the server is up:
1. On the server: `docker compose -f deploy/docker-compose.yml exec admin python -m jobbot user key you@example.com --name vercel-cron`
2. On the PC: `python deploy/vercel_connect.py --host DOMAIN --api-key KEY` (fills in the server's address in
   both `vercel.json` files, sets the Vercel variables and redeploys). Commit the two `vercel.json` files.
3. On the server: set `JOBBOT_PUBLIC_URL=https://jobbot-portal.vercel.app` in `deploy/.env` and restart, so
   payment and mailbox sign-in returns land on the Vercel address.

Redeploy a site by hand: `npx vercel deploy --prod` in its folder (both folders are linked to their projects).
The Caddyfile hands Vercel's `X-Vercel-Forwarded-For` on as `X-Forwarded-For` so sign-in limits count
visitors rather than Vercel; someone calling the server directly could set that header themselves.
