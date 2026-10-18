# Putting the Job Bot on a server

**Status: written, not yet run.** Docker is not installed on the PC this was written on, so the image
has never been built. Expect to fix small things the first time. Everything else in the bot
(sign-in, accounts, schedules, the API) is tested on Windows; the Linux-specific parts are not.

## What you get

One server, reachable at `https://your-domain`, that you can open from a phone:

- the admin page at `/admin` (sign in, then Schedule, Review, Jobs, Sources, Settings);
- the API at `/docs`;
- the scheduler, which starts each account's runs when they fall due.

The bot itself is reachable only by the HTTPS proxy (Caddy), which gets its certificate by itself.

## What you need

| Thing | Notes |
|---|---|
| A Linux server (VPS) | 2 CPU, 4 GB memory is enough to start; Chrome needs the memory. About US$20 to 40 a month |
| A domain name | An `A` record pointing at the server, for example `bot.example.com` |
| Docker with the compose plugin | Installed on the server |
| Ports 80 and 443 open | For the proxy and its certificate |

## Steps

1. Copy the `jobbot` folder to the server. Leave out `.env`, the `.db` files, `output`, `reports`, `logs`, `accounts` and `profile`: they are your own data and are not part of the image.
2. In the `jobbot` folder on the server:

   ```
   DOMAIN=bot.example.com docker compose -f deploy/docker-compose.yml up -d --build
   ```

3. Create the first user. This is the service operator; it must be done on the server itself, because the first-user form is refused over the network on purpose:

   ```
   docker compose -f deploy/docker-compose.yml exec app python -m jobbot user add you@example.com
   ```

4. Open `https://bot.example.com/admin` and sign in.
5. Put the AI key on the server: a file `/data/.env` inside the `data` volume with `GEMINI_API_KEY=...` (and `ANTHROPIC_API_KEY=...` if used).
6. Add a customer: `... exec app python -m jobbot account add "Name" --timezone Europe/Berlin`, then `... exec app python -m jobbot user add them@example.com --account 2`.

## Where the data is

Everything that must be kept is in the Docker volume `data` (mounted at `/data`):

| Path | What |
|---|---|
| `/data/control.db` | Accounts, users, API keys, schedules, runs, audit log |
| `/data/jobbot.db`, `/data/output`, ... | The first account's own data |
| `/data/accounts/<name>/` | Each further customer's database and files |
| `/data/.env` | The service's AI keys |

Back up that volume. There is no automatic backup yet (Phase 3 of the plan).

## Before you publish the image anywhere

`config.yaml` is the master list of settings and still carries your own sending addresses as
defaults. New accounts do not get them (tested), but they are in the file. Blank `sending.send_from`
and `sending.cc` in the copy you put on the server.

## Moving your own data to the server

Not automated yet. Your own bot can keep running on the laptop while customers use the server.
To move it: stop the bot, then copy `jobbot.db`, `output`, `reports` and your `.env` into `/data`.

## Not done yet

- Customers cannot connect a mailbox themselves (Phase 4), so a new account cannot send.
- No automatic backups, monitoring or alerts (Phase 3).
- No payment or sign-up page (Phase 5).
