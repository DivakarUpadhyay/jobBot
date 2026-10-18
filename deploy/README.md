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
