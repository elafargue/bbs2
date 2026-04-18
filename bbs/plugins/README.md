# Writing a BBS2 Plugin

This document explains how to add a new plugin — a self-contained feature that integrates with the BBS session engine, the YAML config, and (optionally) the web dashboard.

---

## 1. Directory layout

Every built-in plugin lives in its own sub-package:

```
bbs/plugins/
    myplugin/
        __init__.py   ← empty, required so Python treats it as a package
        myplugin.py   ← plugin class lives here
```

The `PluginRegistry` walks `bbs/plugins/` with `pkgutil.walk_packages` at startup, imports every module it finds, and instantiates every concrete subclass of `BBSPlugin` it encounters.  No registration step is needed — just drop the package here and it will be loaded automatically.

---

## 2. Minimal plugin skeleton

```python
# bbs/plugins/myplugin/myplugin.py
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from bbs.core.plugin_registry import BBSPlugin

if TYPE_CHECKING:
    from bbs.core.session import BBSSession


class MyPlugin(BBSPlugin):
    # ── Required class attributes ──────────────────────────────────────────
    name            = "myplugin"          # unique key used in config & API
    display_name    = "My Plugin"         # shown in the BBS main menu
    menu_key        = "MP"                # shortcut typed by the user (unique)
    min_auth_level_name = "IDENTIFIED"    # minimum level to see this in the menu
                                          # choices: "ANONYMOUS", "IDENTIFIED",
                                          #          "AUTHENTICATED", "SYSOP"

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        """Called once at BBS startup with the plugin's config sub-section."""
        await super().initialize(cfg, db_path)
        # cfg mirrors the bbs.yaml plugins.myplugin section (dict, may be empty)
        # db_path is the path to the shared SQLite database file
        self._some_setting = cfg.get("some_setting", "default")

    async def handle_session(self, session: "BBSSession") -> None:
        """
        Called every time a user selects this plugin from the main menu.
        Must return when the user is done (do not call sys.exit or raise).
        """
        term = session.term
        await term.sendln(f"Hello from MyPlugin!  Setting: {self._some_setting}")
        await term.sendln()

    async def shutdown(self) -> None:
        """Called on graceful BBS shutdown.  Release external resources here."""

    # ── Web dashboard stats (optional) ────────────────────────────────────
    def get_stats(self) -> dict[str, Any]:
        """
        Return a JSON-serialisable dict.  Shown as-is in the Plugins page
        of the web dashboard (keys other than name/display_name/enabled are
        rendered as a formatted JSON block).
        """
        base = super().get_stats()          # includes {"enabled": ..., "name": ...}
        base["display_name"] = self.display_name
        base["some_setting"] = self._some_setting
        return base
```

---

## 3. Session API quick reference

Inside `handle_session`, `session` exposes:

| Attribute | Type | Description |
|---|---|---|
| `session.term` | `Terminal` | Send/receive text, pagination |
| `session.auth` | `AuthState` | `.callsign`, `.level`, `.user_id` |
| `session.auth_service` | `AuthService` | Re-authentication helpers |
| `session.db` | `aiosqlite.Connection` | Open DB connection (shared with session) |
| `session.plugin_state` | `dict` | Per-session scratch space keyed by plugin name |
| `session.cfg` | `BBSConfig` | Full BBS config object |

Useful `Terminal` methods:

```python
await term.sendln("text")           # send a line (appends \r\n)
await term.send("text")             # send without newline
await term.flush()                  # flush output buffer
line = await term.readline()        # wait for a line of user input
await term.paginate(lines)          # paginate a list of strings
```

---

## 4. Config integration

Add a section under `plugins:` in `bbs.yaml` (and `bbs.yaml.example`):

```yaml
plugins:
  myplugin:
    enabled: true
    some_setting: "hello"
    page_size: 20
```

The entire `myplugin:` dict is passed as `cfg` to `initialize()`.  
If the section is absent the plugin still loads with an empty `cfg`.  
`enabled: false` disables the plugin without unloading it.

---

## 5. Own SQLite schema

If the plugin needs persistent storage, run a schema migration inside `initialize()`:

```python
import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS myplugin_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign   TEXT    NOT NULL COLLATE NOCASE,
    data       TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
"""

async def initialize(self, cfg, db_path):
    await super().initialize(cfg, db_path)
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
```

Use the same `db_path` that was handed to `initialize()`; it is the same file as `session.db`, so queries from a live session and from `initialize()` share the same database.

---

## 6. External / installable plugin

A plugin can also live in a separate Python package and be registered via a `pyproject.toml` entry point:

```toml
[project.entry-points."bbs2.plugins"]
myplugin = "mypkg.plugin:MyPlugin"
```

Install the package into the same virtual environment as `bbs2` and it will be discovered automatically alongside the built-in plugins.

---

## 7. Adding a web dashboard configuration panel

The Plugins page (`vue-app/src/views/Plugins.vue`) renders one card per plugin.  The "configure" button for a plugin is wired in **manually** — follow the pattern used by the `bulletins` plugin.

### 7a. Backend: add REST routes

Create `server/routes/myplugin.py` (sysop-authenticated):

```python
# server/routes/myplugin.py
from flask import jsonify, request, session
from server.app import app

def _require_sysop():
    if not session.get("sysop"):
        return jsonify({"error": "Unauthorized"}), 401
    return None

@app.route("/api/myplugin/items", methods=["GET"])
def myplugin_list():
    err = _require_sysop()
    if err:
        return err
    # query DB or plugin state and return JSON
    return jsonify([])

@app.route("/api/myplugin/items", methods=["POST"])
def myplugin_create():
    err = _require_sysop()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    # validate + insert
    return jsonify({"ok": True})
```

Register the blueprint by importing it in `server/web_interface.py` (or wherever the other route modules are imported):

```python
import server.routes.myplugin   # noqa: F401  — registers routes
```

### 7b. Frontend: create a config component

Add `vue-app/src/views/MyPlugin.vue` as a Vue 3 / Vuetify component.  Follow the pattern in `Bulletins.vue`: fetch from your new API endpoints, display data in a `v-data-table`, and provide create/edit/delete dialogs.

### 7c. Wire the "Configure" button into Plugins.vue

In `vue-app/src/views/Plugins.vue`:

1. Import your component at the top of `<script setup>`:

```js
import MyPluginConfig from './MyPlugin.vue'
```

2. Add a dialog ref:

```js
const myPluginDialog = ref(false)
```

3. Add the "Configure" button inside the plugin card actions — guard it with `v-if="p.name === 'myplugin'"`:

```html
<v-btn
  v-if="p.name === 'myplugin'"
  variant="tonal"
  color="primary"
  append-icon="mdi-cog"
  @click="myPluginDialog = true"
>
  Configure
</v-btn>
```

4. Add the modal at the bottom of the template (alongside the existing bulletin-areas dialog):

```html
<v-dialog v-model="myPluginDialog" max-width="860" scrollable>
  <v-card>
    <v-card-title class="d-flex align-center">
      <v-icon start>mdi-cog</v-icon>
      My Plugin Configuration
      <v-spacer />
      <v-btn icon="mdi-close" variant="text" @click="myPluginDialog = false" />
    </v-card-title>
    <v-divider />
    <v-card-text class="pa-4">
      <MyPluginConfig />
    </v-card-text>
  </v-card>
</v-dialog>
```

### 7d. Rebuild the frontend

```bash
cd vue-app && npm run build
```

The compiled assets land in `static/assets/` and are served by Flask.

---

## 8. Checklist for a new plugin

- [ ] `bbs/plugins/myplugin/__init__.py` (empty)
- [ ] `bbs/plugins/myplugin/myplugin.py` — subclass of `BBSPlugin`
- [ ] Unique `name`, `display_name`, `menu_key`
- [ ] `initialize()` calls `await super().initialize(cfg, db_path)`
- [ ] `handle_session()` returns (does not loop forever without an exit path)
- [ ] Config section added to `config/bbs.yaml.example` under `plugins:`
- [ ] (optional) `get_stats()` returns extra fields for the dashboard
- [ ] (optional) REST routes in `server/routes/myplugin.py`
- [ ] (optional) Vue config component + Plugins.vue wiring
- [ ] (optional) Tests in `tests/test_myplugin.py`
