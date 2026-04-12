"""
bbs/core/plugin_registry.py — Plugin base class and registry.

Plugin contract
---------------
A plugin subclasses BBSPlugin and is placed in bbs/plugins/<name>/.
The registry scans the bbs/plugins/ package at startup and instantiates
every class that is a concrete subclass of BBSPlugin.

Plugins can also be installed as separate packages that declare the
entry point group  bbs2.plugins  in their pyproject.toml:

    [project.entry-points."bbs2.plugins"]
    my_plugin = "my_package.plugin:MyPlugin"

The registry merges both sources.

Access control
--------------
Each plugin declares a  min_auth_level  attribute.  The session manager
only shows the plugin in the main menu if the current session meets the
minimum level.  The plugin's  handle_session()  should re-verify.

Plugins receive a BBSSession and can:
  - Read/write to session.term (Terminal)
  - Check session.auth (AuthState + AuthLevel)
  - Call session.auth_service for re-auth checks
  - Access session.db (open aiosqlite.Connection)

Stats
-----
get_stats() returns a JSON-serialisable dict; the web dashboard polls this.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import pkgutil
from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bbs.core.auth import AuthLevel
    from bbs.core.session import BBSSession
    from bbs.config import BBSConfig

logger = logging.getLogger(__name__)


class BBSPlugin(ABC):
    """Abstract base class for all BBS plugins."""

    #: Unique identifier (used in config and API)
    name: str = ""
    #: Display name in menus
    display_name: str = ""
    #: Single letter / short string shown in menu (e.g. "B", "C")
    menu_key: str = ""
    #: Minimum AuthLevel required to see this plugin in the menu
    #: Import lazily to avoid circular import
    min_auth_level_name: str = "IDENTIFIED"  # name instead of enum to avoid circular

    def __init__(self) -> None:
        self.enabled: bool = True
        self._cfg: dict[str, Any] = {}

    async def initialize(self, cfg: dict[str, Any], db_path: str) -> None:
        """
        Called once at BBS startup.  *cfg* is the plugin's sub-section from
        bbs.yaml.  *db_path* is the SQLite database path for plugin-specific
        schema setup.
        """
        self._cfg = cfg
        self._db_path = db_path

    @abstractmethod
    async def handle_session(self, session: "BBSSession") -> None:
        """
        Take over a user session until the user exits the plugin.
        Must return control to the main menu when done.
        """

    async def shutdown(self) -> None:
        """Called on graceful BBS shutdown."""

    def get_stats(self) -> dict[str, Any]:
        """
        Return a JSON-serialisable stats dict for the web dashboard.
        Override to provide plugin-specific metrics.
        """
        return {"enabled": self.enabled, "name": self.name}

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} key={self.menu_key!r} enabled={self.enabled}>"


class PluginRegistry:
    """
    Discovers, instantiates, and manages all BBS plugins.
    """

    def __init__(self, cfg: "BBSConfig") -> None:
        self._cfg = cfg
        self._plugins: dict[str, BBSPlugin] = {}  # name → plugin

    async def load_plugins(self) -> None:
        """
        Discover and initialise all plugins.  Called once at startup.
        """
        classes: list[type[BBSPlugin]] = []

        # 1. Scan bbs/plugins/ package
        classes.extend(self._scan_builtin_plugins())

        # 2. Entry-point plugins from installed packages
        classes.extend(self._scan_entry_point_plugins())

        # Deduplicate: a class can appear more than once when a plugin package's
        # __init__.py re-exports the class AND the inner module is also walked.
        seen: set[type] = set()
        unique: list[type[BBSPlugin]] = []
        for cls in classes:
            if cls not in seen:
                seen.add(cls)
                unique.append(cls)
        classes = unique

        # 3. Instantiate and initialise
        plugin_cfg = self._cfg.plugins
        db_path = str(self._cfg.db_path)

        for cls in classes:
            try:
                plugin = cls()
                if not plugin.name:
                    logger.warning("Plugin %s has no name — skipping", cls)
                    continue

                # Respect enabled flag from config
                section = plugin_cfg.get(plugin.name, {})
                if not section.get("enabled", True):
                    logger.info("Plugin %s disabled in config", plugin.name)
                    plugin.enabled = False

                await plugin.initialize(section, db_path)
                self._plugins[plugin.name] = plugin
                logger.info(
                    "Loaded plugin: %s [%s] enabled=%s",
                    plugin.name,
                    plugin.menu_key,
                    plugin.enabled,
                )
            except Exception:
                logger.exception("Failed to load plugin %s", cls)

    def _scan_builtin_plugins(self) -> list[type[BBSPlugin]]:
        """Walk bbs.plugins sub-packages, import them, collect BBSPlugin subclasses."""
        import bbs.plugins as plugins_pkg

        found: list[type[BBSPlugin]] = []
        for importer, modname, ispkg in pkgutil.walk_packages(
            path=plugins_pkg.__path__,
            prefix=plugins_pkg.__name__ + ".",
            onerror=lambda n: logger.warning("Error walking plugin package %s", n),
        ):
            try:
                mod = importlib.import_module(modname)
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, BBSPlugin)
                        and obj is not BBSPlugin
                        and not getattr(obj, "__abstractmethods__", None)
                    ):
                        found.append(obj)
            except Exception:
                logger.exception("Error importing plugin module %s", modname)
        return found

    def _scan_entry_point_plugins(self) -> list[type[BBSPlugin]]:
        """Load any installed packages that register 'bbs2.plugins' entry points."""
        found: list[type[BBSPlugin]] = []
        try:
            eps = importlib.metadata.entry_points(group="bbs2.plugins")
            for ep in eps:
                try:
                    cls = ep.load()
                    if issubclass(cls, BBSPlugin) and cls is not BBSPlugin:
                        found.append(cls)
                except Exception:
                    logger.exception("Error loading entry-point plugin %s", ep.name)
        except Exception:
            pass
        return found

    # ── Runtime access ────────────────────────────────────────────────────────

    def get_by_key(self, key: str) -> Optional[BBSPlugin]:
        """Return the enabled plugin whose menu_key matches *key* (case-insensitive)."""
        key = key.upper()
        for p in self._plugins.values():
            if p.enabled and p.menu_key.upper() == key:
                return p
        return None

    def menu_items(self, current_level: "AuthLevel") -> list[tuple[str, str]]:
        """
        Return (key, display_name) pairs for plugins visible at *current_level*.
        """
        from bbs.core.auth import AuthLevel
        items = []
        for p in sorted(self._plugins.values(), key=lambda x: x.menu_key):
            if not p.enabled:
                continue
            required = AuthLevel[p.min_auth_level_name]
            if current_level.value >= required.value:
                items.append((p.menu_key, p.display_name))
        return items

    def toggle(self, plugin_name: str, enabled: bool) -> bool:
        """Enable or disable a plugin at runtime.  Returns False if not found."""
        if plugin_name not in self._plugins:
            return False
        self._plugins[plugin_name].enabled = enabled
        logger.info("Plugin %s %s", plugin_name, "enabled" if enabled else "disabled")
        return True

    def all_stats(self) -> list[dict[str, Any]]:
        """Return stats dicts for all plugins (for web dashboard)."""
        return [p.get_stats() for p in self._plugins.values()]

    def __iter__(self):
        return iter(self._plugins.values())
