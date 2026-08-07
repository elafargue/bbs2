# Signal-quality ingestion & display (Direwolf AGWPE extension)

Status: **Parts A–E DONE** (A–C validated on-air; D = heard-list web column +
twist glyph; E = BBS text-UI column). Only optional map-marker coloring is left.
(Part A = Direwolf fork.)
Companion note on the Direwolf side: `direwolf/AGWPE-SIGNAL-QUALITY-EXTENSION.md`
(on the fork's `agwpe-signal-quality` branch, layered on `udp-audio-out`).

## Goal

Surface Direwolf's per-frame RF signal metrics (`rec`, `mark`, `space`,
`retries`) in bbs2 — recorded per **directly-heard** station and displayed in
both the web heard plugin and the BBS text UI.

## Why an AGWPE extension is required

Direwolf computes a rich per-frame metric (`ax25_alevel_to_text` →
`rec(mark/space)`, plus a retry/FEC "copy effort" level), but it only reaches
two exits, **neither of which is the AGWPE API bbs2 uses**:

| Exit | Coverage | Structured | Level? | bbs2 sees it? |
|------|----------|-----------|--------|---------------|
| AGWPE monitor `'U'/'S'/'I'` | all frames | wire | **no** | ✅ (what bbs2 reads) |
| stdout "HEARD line" | all frames | human text | yes | ❌ |
| CSV log (`LOGDIR`) | **APRS only** | CSV | yes | ❌ |

Verified in the fork: `server.c` builds monitor frames with **no** `alevel`
(the only `alevel` there is a throwaway `memset(...,0xff)` for parsing outgoing
frames), and the CSV/`mheard` path is gated by `if (ax25_is_aprs(pp))`
(= UI frame + PID 0xF0), so it misses connected-mode and NET/ROM (PID 0xCF)
stations — exactly the ones a BBS cares about. The stdout line covers everything
but is human text on a stream bbs2 doesn't own.

Conclusion: extend the AGWPE monitor path (the one universal, structured feed
bbs2 already consumes) to carry the level in-band.

## Metric semantics

`alevel_t { int rec; int mark; int space; }` for 1200-baud AFSK:

- `rec` — received **audio level**, ~0–100 (½ peak-to-peak). Direwolf guidance:
  aim ~50; **>110 too hot** (distortion), **<5 too low**. A *relative*
  strength proxy, **not calibrated RSSI/dBm** — comparable only across stations
  heard by the *same* receiver.
- `mark` / `space` — the two AFSK tone-filter levels; their **balance** reflects
  tuning/multipath (ideal ≈ equal). `-1` for non-AFSK (9600/PSK/FM) → N/A.
- `retries` — decode-effort/FEC level; **0 = clean copy**, higher = marginal.

## Wire extension (backwards-compatible by construction)

**Contract:** a stock AGWPE client at the patched Direwolf must get
**byte-identical** frames; bbs2 at a stock Direwolf must degrade cleanly.

**Carrier:** the AGWPE header's `user_reserved` field (`struct agwpe_s`, bytes
32–35), which `server_send_monitored` already `memset`s to 0. On `'U'/'S'/'I'`
monitor frames, for opted-in clients only, it becomes four raw bytes:

```
user_reserved[0] = rec       (clamped 0..255)
user_reserved[1] = mark      (clamped 0..255; 0xFF = N/A, non-AFSK)
user_reserved[2] = space     (clamped 0..255; 0xFF = N/A)
user_reserved[3] = retries   (0 = clean copy)
```

Written as raw bytes (no `host2netle`) → no endianness ambiguity. The monitor
**text payload is untouched**; only previously-zero header bytes are filled.

**Handshake (opt-in + ACK):**
1. New client→server datakind **`'q'`** ("enable extended signal reporting") —
   unused in the incoming set. bbs2 sends it once, right after `'m'`.
2. Patched Direwolf sets a per-client flag `enable_ext_sig_to_client[client]`
   and replies with one **ACK** frame (datakind `'q'`, payload `"ExtSig=1"`).
3. bbs2 activates extended parsing **only after the ACK**. A stock Direwolf logs
   `'q'` as `**INVALID**` (benign default), never flags the client, never ACKs,
   and keeps `user_reserved = 0`.

Why airtight: population happens inside the existing `for (client…)` loop in
`server_send_monitored`, *after* the `memset`, *only* when the flag is set — a
non-opted client's frame is bit-for-bit unchanged. The opt-in is
fire-and-continue (no blocking wait), so bbs2 ↔ stock never stalls.

Rejected alternative: a separate `'q'` data-frame per monitor frame (richer
payload, but doubles frame count and needs correlation). Four small numbers fit
the reserved bytes; revisit only if we later ship the spectrum string.

## NET/ROM frames (NODES broadcasts)

NET/ROM `'U'` frames (PID 0xCF or dest `NODES`) are short-circuited to the routing
observer and used to `return` before `on_heard` — so a node known only through
NODES broadcasts got no level, even though Direwolf measured one (e.g. KI6ZHD-5
"SCLARA", heard at 54–57, showed `signal_level: null`). Fixed: after the routing
observer, the transport also records the RF signal for the transmitter via
`on_heard(..., info="", count_it=False)` — beacon text suppressed (payload is
binary routing data) and **the observation not counted** (the routing path already
counts it on the `netrom` row), so the level lands on the `agwpe` row without
double-counting. Gated on a decoded signal, so it's a no-op without the extension.
Attribution (direct source / last-hop digi) is done in the transport, which still
has the via-path (the router's `on_netrom_nodes` doesn't).

## Connected-mode callers (live self-signal)

A station *connected* to the BBS arrives as `'C'`/`'D'` (session) frames, so it was
never signal-logged — a caller couldn't see their own level in the Signals view.
But Direwolf also monitors its `SABME`/`RR`/`I` frames as `'S'`/`'I'` frames, each
carrying `user_reserved`. So the transport now records the sender's signal off
`'S'`/`'I'` frames too, via `on_heard(..., info="", count_it=False, log_signal=False)`:
real-time (every frame updates `last_level`), uncounted (a session is many frames,
not many observations), and quiet (`log_signal=False` — no per-frame journal spam;
the Signals view is the readout). Same direct-source / last-hop-digi attribution.
Result: a connected op can tune their radio and re-read `S` to watch their level
change live. No-op without the extension.

## Attribution rule

Signal quality is a property of the **direct RF hop**, never a digipeated
origin. bbs2 attaches the level to the station it **directly heard** (last
H-bit-set repeater in the via path, else the source) — reusing the existing
`last_direct_heard` logic. Digipeated-only receptions store no level.

## bbs2 changes

**B. Transport** (`bbs/transport/agwpe.py`) — DONE
- Sends `'q'` after `'m'` in the `'X'` ack (config `transports.agwpe.signal_quality`,
  default true); `_ext_sig_active` flips only on the inbound `'q'` ACK.
- `_read_loop` captures the 4 `user_reserved` bytes and passes them to
  `_dispatch`; `_decode_signal()` turns them into `(rec, mark, space, retries)`
  (all-zero/short → None). On `'U'` heard frames, when active, the signal is
  passed to `_heard_observer` (7th arg; KISS keeps 6 via the default).
- `HeardFrameCallback` (base.py) extended with the optional trailing `signal`.

**C. Heard schema v5** (`bbs/plugins/heard/heard.py`) — DONE (storage)
- `_HEARD_SCHEMA_VERSION` 4 → 5; `_heard_migration_v5` guarded `ALTER TABLE
  heard_events ADD COLUMN` for `last_level`, `last_tone_mark`,
  `last_tone_space`, `last_copy_quality`, `best_level` (also in `_SCHEMA` for
  fresh DBs). Signal lives on `heard_events` (a reception property), not
  `stations`; `on_heard(..., signal=None)` writes `last_*` + `best_level =
  MAX(...)`, tone `0xFF` → NULL.
- **Attribution** = the station whose RF we actually received:
  - direct frame → the **source** row (transport = the real transport);
  - digipeated frame → the **last H-bit-set digi** (`_last_star`) row
    (transport = `''`, alias-resolved to the owner). Each retransmission Direwolf
    hears is a separate `'U'` frame carrying that hop's level, so every relaying
    digi keeps its own signal current (incl. our own beacon bounced back).
- Tests: `tests/test_heard_signal.py` (source + digi attribution, migration) +
  `tests/test_agwpe.py` `TestDecodeSignal`/`TestSignalQualityExtension`.

**D. Web UI** — heard list DONE
- `/api/heard` exposes aggregated signal per callsign: `signal_level`/`_tone_mark`/
  `_tone_space`/`_copy` from the freshest signal-bearing row, `signal_best` =
  `MAX(best_level)` across the callsign's rows (so a station heard both directly
  and as a digi merges correctly — the two-row case).
- `HeardConfig.vue` "Signal" column: single-hue magnitude bar (`v-progress-linear`,
  level as %) + numeric + a **diverging tone-balance ("twist") glyph**
  (`20·log10(mark/space)`, ±6 dB clamp, warm #b5651d = low-tone heavy / cool
  #2a9d8f = high-tone heavy, centre tick; direction also in the tooltip so it's
  not colour-alone) + a reserved warning icon when copy is marginal. Tooltip:
  level · twist (dB + lean) · mark/space · best · copy. Magnitude → sequential
  single hue, twist → diverging pair (validated light+dark), per the dataviz method.
  Twist reflects sender TX pre-emphasis/deviation at our (constant) receiver — a
  *relative* diagnostic (local cluster ≈ +2–3.5 dB low-tone-heavy at radiostation2).
- Tests: `tests/test_heard_signal.py` `test_api_heard_*` (exposure, null-absent,
  cross-row aggregation). Remaining: optional map-marker coloring by level.

**E. BBS text UI** — DONE (dedicated Signals view)
- The `H`/`HS` heard listings are left unchanged — signal exists only for the
  directly-heard subset, so a column there was mostly-empty noise on long AX.25
  lists. Instead a new **`S` (Signals)** menu command lists **only** stations we
  have a level for, strongest first, with room for level *and* twist:
  `CALLSIGN · LAST HEARD · SIGNAL · TWIST`.
  - SIGNAL = `_fmt_signal_cell` (5-seg **ASCII** magnitude bar + level + `~`
    marginal marker; padded before styling so columns align; ASCII-only since BBS
    output is ascii-encoded).
  - TWIST = `_fmt_twist` = `20·log10(mark/space)` signed dB + heavier tone
    (`+3.5 dB mark` / `-4.9 dB space` / `+0.0 dB even` / `n/a` for non-AFSK).
- The `S` item appears only when `_signal_station_count > 0` (extension active),
  so stock setups don't see it. Tests: `test_fmt_signal_cell_*`, `test_fmt_twist_*`.
- Note: found a pre-existing bug in `Terminal._visible_len` (strips `\x1b` before
  the CSI regex, so it under-counts truecolor SGR codes → wrong padding in
  `send_menu` two-column layout on the web terminal). Not touched here; left for a
  separate fix. Our signal cell doesn't rely on it (pads raw before styling).

## Testing

- **Unit:** synthetic `'U'` frame with populated `user_reserved` → assert
  extraction, direct-hop attribution, v5 storage. Compat: `user_reserved=0` and
  `_ext_sig_active=False` → no signal, no error.
- **Integration:** extend the `tests/rig` Direwolf harness (which runs this
  fork) to assert bbs2 records a level end-to-end (levels near-constant under
  fixed UDP audio — proves the plumbing).
- **On-air:** build the fork, run bbs2 against the real radio, confirm sensible
  per-station levels; point a stock AGWPE client at the same Direwolf and
  confirm it's unaffected.

## Edge cases

- Non-AFSK: only `rec` valid; mark/space = `0xFF` → UI hides tone balance.
- Own-TX `'T'` / APRStt: sentinel `rec=-1` → no bytes written, skipped.
- `mark`/`space` >255: clamped (lossy only at extremes).
- Datakind `'q'` collision with a future official AGWPE command: handshake fails
  safe (no activation).
