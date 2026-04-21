# Daycare

A personal assistant that syncs your CalDAV calendar and relentlessly escalates reminders via Signal and email — from a gentle heads-up, through increasingly firm nudges after the deadline, all the way to calling you out on how many times you've rescheduled something. You interact with it through Signal messages to manage todos and events, save ideas, and track what's in progress. It runs as a self-hosted Docker stack with Radicale as the calendar backend and a lightweight web dashboard for a quick overview.

## Architecture

```
Radicale (CalDAV)
       │
       ▼
 caldav_sync.py ──► state.json ◄──► receiver.py  ◄── Signal (incoming commands)
                         │
                         ▼
                   notifier.py ──► Signal / Email (outgoing reminders)
                   cleanup_runner.py ──► cleanup.py ──► archive_YYYY-MM.json

ideas.json  ◄──► receiver.py  (add,idea / list idea)

dashboard (nginx) ──► state.json + ideas.json  (read-only web dashboard)
```

## Services (Docker Compose)

| Container | Image | Purpose |
|---|---|---|
| `daycare` | local build | CalDAV sync, notifier, receiver, cleanup |
| `radicale` | tomsquest/docker-radicale | CalDAV server |
| `signal-cli-rest-api` | bbernhard/signal-cli-rest-api | Signal messenger bridge |
| `traefik` | traefik | Reverse proxy / TLS for Radicale / Dashboard (self-signed certs)|
| `dashboard` | nginx:alpine | Read-only web dashboard (port 8082), served from `container/nginx/` |

## Python Scripts

All scripts live in `container/daycare/build/`.

| File | Role |
|---|---|
| `caldav_sync.py` | Polls Radicale every N minutes, writes todos+events to `state.json` |
| `notifier.py` | Checks `state.json` every minute, sends escalation reminders via Signal/email |
| `receiver.py` | Polls Signal every 5 s for incoming commands, mutates `state.json` and `ideas.json` |
| `cleanup_runner.py` | Daemon that triggers `cleanup.py` once daily at the configured time |
| `cleanup.py` | Archives completed, deleted, and overdue items from `state.json` to `archive_YYYY-MM.json` |
| `logger.py` | Shared CSV logger — all scripts log to stdout in the same format |
| `json_utils.py` | Shared JSON helper — `dump()` wraps `json.dumps(indent=2)` and compacts `sent_levels` entries to one line each |
| `utils.py` | Shared utilities — `read_secret`, `locked_read_json`, `locked_write_json`, `send_signal` |

## Data Files

All written to `container/daycare/data/` (mounted as `/data` inside the container).

| File | Written by | Read by |
|---|---|---|
| `state.json` | `caldav_sync`, `notifier`, `receiver`, `cleanup` | all scripts + dashboard |
| `ideas.json` | `receiver` | `receiver` + dashboard |
| `archive_YYYY-MM.json` | `cleanup` | `receiver` (search) |

### state.json entry structure

```json
{
  "uid": {
    "title": "HOME: Buy milk",
    "short_id": "a1b2c3",
    "due": "2026-04-20T10:00:00+02:00",
    "due_previous": null,
    "reschedule_count": 0,
    "type": "todo",
    "completed": false,
    "in_progress": false,
    "deleted": false,
    "sent_levels": [
      {"level": 1, "sent_at": "2026-04-20T09:30:00+02:00"}
    ]
  }
}
```

`deleted` is set to `true` by `caldav_sync` when an item with a future due date disappears from CalDAV (e.g. deleted in the calendar app). Deleted items are hidden from all list commands, receive no further reminders, and are archived by the cleanup on its next run.

### ideas.json entry structure

```json
[
  {
    "id": "a1b2c3",
    "date": "2026-04-20T09:00:00+02:00",
    "idea": "HOME: Build a new shelf",
    "status": ""
  }
]
```

`status` is one of `""` (open), `"done"`, `"rejected"`.

## Configuration

### Radicale setup

Follow the instructions from https://github.com/tomsquest/docker-radicale to get your server up and running.

the config file is ready to be used. "Only" your user needs to be created.

### Signal CLI Rest APi

https://github.com/bbernhard/signal-cli-rest-api/blob/master/README.md

Therefore open http://CHANGEME-TO-FQDN-OR-IP:8080/v1/qrcodelink?device_name=signal-api in your browser, open Signal on your mobile phone, go to Settings > Linked devices and scan the QR code using the + button.

### `container/daycare/config/config.yml`

```yaml
caldav:
  url: "https://..."
  username: "user@example.com"
  ssl_verify: "/certs/ca.pem"        # path or true/false

signal:
  api_url: "http://signal-cli-rest-api:8080"
  sender: "+phonenumber"             # your phone number
  recipient: "note-to-self"          # or a phone number

email:
  smtp_host: "mail.example.com"
  smtp_port: 587
  smtp_user: "daycare@example.com"
  smtp_tls: true
  from: "daycare@example.com"
  to: "you@example.com"

caldav_sync:
  sync_interval_minutes: 15
  lookahead_hours: 24        # display filter for list/list todo/list event (list all ignores this)
  timezone: "Europe/Berlin"

notifier:
  check_interval_minutes: 1

receiver:
  list_all_max_lines: 40     # if list all exceeds this, remainder is sent via email

escalation:
  - level: 1
    offset_minutes: -30              # 30 min before due
    channels: ["signal"]
    message_style: "friendly"
    repeat: false
  - level: 5
    offset_minutes: 240              # 4 h after due
    channels: ["signal"]
    message_style: "persistent"
    repeat: true
    repeat_interval_minutes: 120

ignore:
  titles: ["lunch", "break"]        # exact matches (case-insensitive)
  contains: ["block", "private"]    # substring matches (case-insensitive)

quiet_hours:
  enabled: true
  from: "19:00"
  to: "07:00"                       # supports overnight ranges

daily_summary:
  enabled: true
  time: "08:00"
  channels: ["signal"]

cleanup:
  enabled: true
  time: "02:00"
  older_than_days: 30

reschedule_alerts:
  channels: ["signal"]

add_item:
  calendar_name: "change_me_to_your_calendar"        # target calendar for new items
  default_duration_minutes: 30
```

### Secrets

Stored as files under `container/secrets/` and mounted via Docker secrets at `/run/daycare-secrets/`.

| File | Used for |
|---|---|
| `container/secrets/calendar_password` | Radicale CalDAV authentication |
| `container/secrets/smtp_password` | SMTP authentication |

## Signal Commands

All commands are case-insensitive and sent as note-to-self.

| Command | Description |
|---|---|
| `add,todo,Title,YYYY-MM-DD HH:MM` | Create a new todo in CalDAV |
| `add,event,Title,YYYY-MM-DD HH:MM` | Create a new event in CalDAV |
| `add,idea,Description` | Save an idea (no date required) |
| `done <id>` | Mark item as completed (also updates CalDAV) |
| `wip <id>` | Mark item as in progress (pauses reminders) |
| `unwip <id>` | Move item back to open |
| `list` | Open todos and events within `lookahead_hours`, sorted by due date |
| `list todo` / `list event` | Filter by type |
| `list all` | Everything including completed items and ideas, sorted by due date — sends email if too long |
| `list idea` | All ideas |
| `list idea open` | Open ideas only |
| `list idea done` | Implemented ideas |
| `list idea 2026-04` | Ideas from a specific month |
| `idea <id> done` | Mark idea as implemented |
| `idea <id> rejected` | Mark idea as rejected |
| `idea <id>` | Clear idea status |
| `search <query>` | Search active items and ideas |
| `search all <query>` | Also search archived items |
| `help` | Show command overview |

## Daycare Dashboard

Read-only web dashboard served by nginx on port 443. Displays todos, events, and ideas from `state.json` and `ideas.json`.

| Path | Purpose |
|---|---|
| `container/nginx/data/index.html` | Dashboard frontend |
| `container/nginx/config/nginx.conf` | nginx server config |

Which items appear is controlled by the `DISPLAY_PREFIXES` array at the top of `container/nginx/data/index.html`:

```js
const DISPLAY_PREFIXES = [
  { prefix: "HOME:", label: "Home" },
  { prefix: "WORK:", label: "Work" },
];
```

Add or remove entries here to control which prefixes are shown. The prefix is stripped from the displayed title and shown as a badge on each card. **Leave the array empty to show all items regardless of prefix** — useful as a starting point (`index.example.html` ships with an empty array for this reason).

## Operations

```bash
# Build and start all services
docker compose up -d --build

# Follow logs
docker compose logs -f

# Follow a single service
docker compose logs -f daycare

# Restart daycare after config change (no rebuild needed)
docker compose restart daycare

# Force rebuild after code change
docker compose up -d --build daycare
```

## Host Setup

Ubuntu 24.04 minimal server.

```bash
sudo apt update
```

### Fail2ban

```bash
sudo apt install fail2ban
sudo vim /etc/fail2ban/jail.local
```

```
[DEFAULT]
# Ban duration in seconds (3600 = 1 hour)
bantime = 3600
# Time window to count failed attempts (600 = 10 minutes)
findtime = 600
# Maximum failed attempts before ban
maxretry = 3
# Ignore localhost
ignoreip = 127.0.0.1/8 ::1

[sshd]
enabled = true
port = 22
filter = sshd
logpath = /var/log/auth.log
maxretry = 3

[radicale]
enabled = true
port = 5232
filter = radicale
logpath = /var/log/syslog
maxretry = 3
```

```bash
sudo vim /etc/fail2ban/filter.d/radicale.conf
```

```
# Fail2ban filter for radicale

[Definition]
failregex = \[WARNING\] Failed login attempt from \S+ \(forwarded for '<HOST>'\)

ignoreregex =
```

```bash
sudo systemctl restart fail2ban

# Verify the filter works against the log
sudo fail2ban-regex /var/log/syslog /etc/fail2ban/filter.d/radicale.conf

# Check ban status
sudo fail2ban-client status radicale
```

### Docker

```bash
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl status docker
```

### rsyslog (Docker log routing)

```bash
sudo apt install rsyslog
```
