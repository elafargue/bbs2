# bbs2 utility scripts

Helper scripts for operating and trying out bbs2.

## `echo_service.py` — external-service demo

A minimal program for trying bbs2's ax25d-style **external-service hosting**
(the `services:` block in `bbs.yaml`). It echoes back whatever the caller
types, so you can confirm a service route works end to end before pointing one
at real software (FBB, `node`, a game, …).

### Quick start

1. Make it executable:
   ```bash
   chmod +x util/echo_service.py
   ```

2. Add a route in `bbs.yaml` — via the web **Services** panel, or by hand —
   on a **spare SSID** (not your BBS's own callsign):
   ```yaml
   services:
     enabled: true
     routes:
       "YOURCALL-9":                              # a spare SSID you own
         exec: /full/path/to/bbs2/util/echo_service.py
         args: ["echo_service", "%U"]             # %U = caller callsign (no SSID)
         crlf: true                               # REQUIRED — see note below
   ```

3. Restart bbs2. (A *new* service SSID has to register with the radio, so it
   needs a restart or a transport reconnect — an existing route's settings
   hot-reload from the web panel.)

4. From a packet client, connect to `YOURCALL-9`. You should get a banner and
   see each line you send echoed back. Send `/q` to disconnect.

Requires the **AGWPE** transport (Direwolf) for direct AX.25 callers; bbs2
registers the service SSID with Direwolf on startup.

### ⚠️ The `crlf: true` note (read this)

AX.25 terminates lines with a bare **CR** (`\r`), not LF (`\n`). Line-oriented
programs — this one, and anything using `readline`/`fgets`/`input()` — read
until a newline, so **without `crlf: true` the program blocks on its first read
and never responds**: the connection succeeds, then goes completely silent.
Native packet apps that handle raw CR themselves (`node`, FBB) use
`crlf: false`.

### Full reference

See the `services:` section of the top-level `README.md` and
`config/bbs.yaml.example` for every option: argv `%`-substitution
(`%u/%U/%s/%S/%d/%%`), `env:`, `lockout`, `no_digi`, `quiet`, `min_auth`, and
`idle_timeout`.
