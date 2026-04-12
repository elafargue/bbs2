# BBS2 — Amateur Radio BBS

A modular, async Bulletin Board System for amateur radio operators, written in Python. Stations connect via packet radio (AX.25) or TCP; a built-in web interface lets the sysop manage users, bulletins, and activity in real time.

```
  ___ ___  ___   ___
 | _ ) _ )/ __| |_  )
 | _ \ _ \\__ \  / /
 |___/___/|___/ /___|

Ed's BBS (W6ELA-1) — Palo Alto, USA
Welcome, W1AW-7!
[B]ulletins [C]hat [LC] Last Connections [Q]uit
```

---

## Features

- **Multiple AX.25 transports** — KISS serial, KISS TCP (Dire Wolf), Linux kernel AF_AX25, and AGWPE/AGW Packet Engine
- **TCP transport** for Telnet access and local development
- **Bulletin Board** — threaded messages organized into areas with per-area read/post access levels
- **Multi-room Chat** — broadcast chat with private messaging and room history
- **Last Connections log** — persistent journal of every station that has visited
- **OTP authentication** — TOTP (RFC 6238) and HOTP (RFC 4226), compatible with any standard authenticator app
- **Web management interface** — Vue 3 + Vuetify SPA with real-time activity feed via WebSocket
- **Beaconing** — periodic UI beacon frames on all radio transports
- **Systemd integration** — production-ready service unit included

---

## Requirements

- **Python 3.9+**
- Linux (for AX.25 transports; TCP/development works on any OS)
- `ax25-tools` + `kissattach` for kernel AX.25 transport
- A TNC or software modem (Dire Wolf, UZ7HO Soundmodem, etc.)

---

## Quick Start (Development)

```bash
# Clone and set up a virtual environment
git clone https://github.com/youruser/bbs2
cd bbs2
python3 -m venv .v && source .v/bin/activate
pip install -e ".[dev]"

# Create your config
cp config/bbs.yaml.example config/bbs.yaml
# Edit config/bbs.yaml — at minimum set bbs.callsign and web.secret_key

# Set the sysop web password
bbs2 --set-sysop-password

# Run in debug mode
bbs2 --debug
```

The BBS listens on TCP port 6300 by default. The web interface is at http://127.0.0.1:8080.

Connect a test client:
```bash
telnet localhost 6300
```

---

## Production Installation (Linux)

```bash
# Build the frontend first
cd vue-app && npm install && npm run build && cd ..

# Install (creates /opt/bbs2, bbs system user, systemd service)
sudo bash install/setup.sh

# Edit the config
sudo nano /opt/bbs2/config/bbs.yaml

# Set the sysop web password
sudo -u bbs /opt/bbs2/venv/bin/bbs2 \
    --config /opt/bbs2/config/bbs.yaml \
    --set-sysop-password

# Start the service
sudo systemctl start bbs2
sudo journalctl -u bbs2 -f
```

The installer:
1. Creates a `bbs` system user (added to the `dialout` group for serial TNC access)
2. Deploys all files to `/opt/bbs2/`
3. Creates a Python venv at `/opt/bbs2/venv/`
4. Installs and enables `bbs2.service` via systemd

---

## Configuration Reference

All configuration lives in a single YAML file (default: `config/bbs.yaml`). A fully annotated example is in `config/bbs.yaml.example`.

### `bbs:`

```yaml
bbs:
  callsign: N0CALL       # Required. Max 6 chars.
  ssid: 1                # 0–15. Appended as "-N" (0 = no SSID).
  name: My BBS           # Shown in the login banner.
  sysop: N0CALL          # Sysop callsign.
  location: Anytown, USA
  max_users: 20          # 0 = unlimited.
  idle_timeout: 300      # Seconds. 0 = no timeout.
```

### `transports:`

Enable one or more transports. All enabled transports run concurrently.

#### KISS over serial port

Connects directly to a hardware TNC or Dire Wolf via a serial port. No `kissattach` required.

```yaml
transports:
  kiss_serial:
    enabled: true
    device: /dev/ttyACM0
    baud: 9600
    port: 0              # AX.25 port number (0–15) inside KISS frames.
    beacon_text: "N0CALL-1 BBS"
    beacon_interval: 20  # Minutes between beacons.
```

#### KISS over TCP

Connects to Dire Wolf's KISS TCP interface (default port 8001). No `kissattach` required.

```yaml
transports:
  kiss_tcp:
    enabled: true
    host: 127.0.0.1
    port: 8001
    ax25_port: 0
    beacon_text: "N0CALL-1 BBS"
    beacon_interval: 20
```

#### Linux kernel AF_AX25

Uses connected-mode AX.25 sockets via the Linux kernel. Requires `kissattach` and a configured `axports` entry. Uses `ctypes`/`libc` directly since Python's `socket` module does not support AF_AX25 address packing.

```yaml
transports:
  kernel_ax25:
    enabled: true
    axport: ax0          # Entry name in /etc/ax25/axports.
    beacon_text: "N0CALL-1 BBS"
    beacon_interval: 20
    beacon_path: ""      # Digipeater path, e.g. "WIDE1-1,WIDE2-1".
```

#### AGWPE / AGW Packet Engine

Connects to an AGWPE-compatible server (Dire Wolf, UZ7HO Soundmodem) via the AGW TCP API. Auto-reconnects with exponential back-off on connection loss.

```yaml
transports:
  agwpe:
    enabled: true
    host: 127.0.0.1
    port: 8000           # AGWPE default TCP port.
    agw_port: 0          # Radio port number (0-based).
    password: ""         # Leave blank if no AGWPE password configured.
    beacon_text: "N0CALL-1 BBS"
    beacon_dest: BEACON  # Destination callsign for beacon UI frames.
    beacon_interval: 20
    beacon_path: ""
```

#### TCP (Telnet)

Plain TCP for Telnet access and testing. No AX.25 involved.

```yaml
transports:
  tcp:
    enabled: true
    host: 0.0.0.0
    port: 6300
```

### `database:`

```yaml
database:
  path: data/bbs.db
  connection_log_days: 30    # Days to keep the connection journal. 0 = keep forever.
```

### `auth:`

```yaml
auth:
  nonce_hex_length: 32       # 32 hex chars = 16 bytes of entropy per challenge.
  max_attempts: 3            # Failed OTP attempts before lockout.
  lockout_seconds: 900       # 15 minutes.
  totp_time_step: 30         # TOTP window in seconds (RFC 6238).
```

### `plugins:`

```yaml
plugins:
  bulletins:
    enabled: true
    max_body_bytes: 4096     # Keep small for 1200 bps links.
    max_subject_chars: 25
    default_areas:
      - name: GENERAL
        description: General discussion
      - name: TECH
        description: Technical topics

  chat:
    enabled: true
    history_lines: 50        # Lines shown to each user on room join.
    default_rooms:
      - name: main
        description: Main chat room

  lastconn:
    enabled: true
    limit: 200               # Max rows shown in the connections list.
```

### `web:`

```yaml
web:
  host: 127.0.0.1
  port: 8080
  secret_key: "CHANGE_ME"   # Required. Random string for session signing.
  sysop_password_hash: ""   # Set with: bbs2 --set-sysop-password
```

### `logging:`

```yaml
logging:
  level: INFO                # DEBUG, INFO, WARNING, ERROR
  file: ""                   # Optional log file path. Blank = stdout/journal only.
```

---

## Authentication

BBS2 uses a four-level access model.

| Level | How obtained |
|---|---|
| **Anonymous** | Any new TCP connection before a callsign is declared |
| **Identified** | Callsign from AX.25 header (trusted by OS/TNC) or self-declared on TCP |
| **Authenticated** | Passed an OTP challenge (TOTP or HOTP) |
| **Sysop** | Special sysop account |

On radio transports (KISS, AGWPE, kernel AX.25), the callsign comes from the AX.25 frame header and is trusted at the OS/TNC level — stations arrive as **Identified** automatically. On TCP, users identify themselves with a callsign and are similarly treated as Identified.

### Setting up OTP for a user

1. Open the web interface → **Users**
2. Create the user or select an existing one
3. Click **Provision OTP Secret** — a TOTP secret is generated, and a `otpauth://` QR code URI is shown
4. The user scans it with Google Authenticator, Authy, or any RFC 6238 app
5. On next login, they will receive an OTP challenge

HOTP (counter-based) is also supported for radio operators who cannot easily use time-synchronized apps.

---

## Plugins

### Bulletins (`B`)

A traditional packet BBS message store, organized into named areas.

| Key | Action |
|---|---|
| `A` | List areas and select one |
| `L` | List messages in the current area |
| `R <#>` | Read message number # |
| `S` | Post a new message |
| `D <#>` | Delete a message (own messages, or any as sysop) |
| `Q` | Return to main menu |

Message posting on radio links uses the `Identified` level. TCP connections require `Authenticated` to post (OTP challenge).

### Chat (`C`)

Multi-room real-time chat. Maximum message length is 160 characters (tuned for 1200 bps links).

| Command | Description |
|---|---|
| `/WHO` | List users in the current room |
| `/MSG <call> <text>` | Send a private message |
| `/JOIN <room>` | Switch to another room |
| `/ROOMS` | List available rooms |
| `/QUIT` | Exit chat |

Each room maintains a scrollback buffer (default 50 lines) shown to users on join.

### Last Connections (`LC`)

Displays a table of all stations that have connected in the past `connection_log_days` days:

```
CALLSIGN  FIRST SEEN        LAST SEEN         TRANSPORT    AUTH
W1AW-3    2026-04-10 14:22  2026-04-12 09:11  agwpe        auth
KD6XYZ    2026-04-11 20:05  2026-04-11 20:05  kiss_tcp     ident
```

Requires `Identified` level. Connections are recorded after each session ends. Anonymous connections (no callsign established) are not recorded.

---

## Web Interface

The sysop web interface is a Vue 3 + Vuetify SPA served from `static/`. It communicates with the BBS over a REST API and a WebSocket (Socket.IO) connection.

**Views:**
- **Dashboard** — connected users, uptime, quick stats
- **Activity** — live scrolling log of all BBS events
- **Users** — create, edit, approve, ban users; provision/revoke OTP secrets
- **Bulletins** — manage areas; view, create, remove messages
- **Plugins** — enable/disable plugins, view per-plugin stats

### Building the frontend

```bash
cd vue-app
npm install
npm run build
```

Built assets are written to `static/`. The Flask server serves `static/index.html` as a fallback for all non-API routes (SPA routing).

---

## Development

### Running tests

```bash
source .v/bin/activate
pytest tests/ -q
```

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`. All async tests run without decoration.

### Test layout

| File | Coverage |
|---|---|
| `tests/test_agwpe.py` | AGWPE frame encoding, beacon, registration |
| `tests/test_auth.py` | TOTP/HOTP computation, lockout logic |
| `tests/test_bulletins.py` | Bulletin DB operations, access control |
| `tests/test_chat.py` | Chat broadcast, private messages, room commands |
| `tests/test_kiss_codec.py` | KISS/AX.25 frame encoding and decoding |
| `tests/test_session.py` | BBS session lifecycle |

### Project layout

```
bbs/
  main.py              Entry point and CLI
  config.py            Configuration loading and validation
  ax25/                AX.25 address utilities and KISS frame codec
  core/
    engine.py          Asyncio BBS engine, session lifecycle
    session.py         Per-connection session state
    auth.py            Authentication levels and OTP verification
    plugin_registry.py Plugin discovery and management
    terminal.py        Line-oriented terminal I/O helpers
  db/
    schema.py          SQLite schema creation and migrations
    users.py           User CRUD helpers
    connections.py     Connection journal helpers
  plugins/
    bulletins/         Bulletin board plugin
    chat/              Multi-room chat plugin
    lastconn/          Last connections plugin
  transport/
    base.py            Abstract transport and Connection types
    tcp.py             TCP (Telnet) transport
    kiss.py            KISS (serial and TCP) transport
    kernel_ax25.py     Linux kernel AF_AX25 transport
    agwpe.py           AGWPE TCP API transport
server/
  app.py               Flask + SocketIO application
  web_interface.py     Web bridge (asyncio → SocketIO)
  routes/              REST API blueprints
  websocket/           SocketIO event handlers
vue-app/               Vue 3 + Vuetify frontend source
config/
  bbs.yaml.example     Annotated config template
install/
  setup.sh             Production installer script
  bbs2.service         Systemd service unit
```

---

## Security Notes

- **`web.secret_key`** must be set to a unique random string before running. The BBS refuses to start with the default placeholder value.
- **Sysop password** is bcrypt-hashed. Set it with `bbs2 --set-sysop-password` (minimum 12 characters enforced).
- **OTP secrets** are stored as raw bytes and are never returned over the REST API.
- **Account lockout** applies after configurable failed OTP attempts (default: 3 attempts → 15-minute lockout).
- The installer sets config directory permissions to `750` and data directory to `700`, owned by the `bbs` user.

---

## License

See [LICENSE](LICENSE) for details.
