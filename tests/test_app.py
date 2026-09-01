"""WP-14 — the Nautilus extension, the composition root, the CLI and packaging.

The headline test is the import boundary. `ext/nautilus_onedriveui.py` runs
inside Nautilus, under the *system* interpreter, and importing anything from the
`onedriveui` package fails at load with **no useful error at all** — Nautilus
logs nothing and the extension simply never appears. So the boundary is asserted
with the AST, and the constants it is forced to duplicate are asserted equal to
the ones in the package.

Then the ordering. `STARTUP_ORDER` describes six constraints that each fail —
some loudly, some silently — if violated, and a comment describing an ordering
constraint is one that gets violated. So the code is checked against it.
"""

from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from onedriveui import __version__, paths
from onedriveui.app import STARTUP_ORDER, Application, SystemdAdapter, build_engine
from onedriveui.models import AccountInfo, SyncState
from onedriveui.strings import FILE_STATE_LABEL
from onedriveui.ui.icons import EMBLEM_FOR_STATE

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT_PATH = REPO_ROOT / "onedriveui" / "ext" / "nautilus_onedriveui.py"
APP_PATH = REPO_ROOT / "onedriveui" / "app.py"
PACKAGING = REPO_ROOT / "packaging"


def ext_source() -> str:
    return EXT_PATH.read_text(encoding="utf-8")


def ext_namespace() -> dict:
    """The extension's module-level constants, without importing `gi`.

    Executed with the `gi` imports stripped, because importing the real one in
    a test process would pull in GTK and Nautilus typelibs that are not
    guaranteed to be present — and the constants are what is being checked.
    """
    tree = ast.parse(ext_source())
    tree.body = [
        node for node in tree.body
        if not (isinstance(node, (ast.Import, ast.ImportFrom))
                and "gi" in ast.dump(node))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                 and ast.dump(node.value).find("require_version") >= 0)
        and not isinstance(node, ast.ClassDef)
    ]
    namespace: dict = {}
    exec(compile(tree, str(EXT_PATH), "exec"), namespace)   # noqa: S102
    return namespace


# ═════════════════════════════════════════════════════════════════════════════
# The import boundary
# ═════════════════════════════════════════════════════════════════════════════

class TestExtensionImports:

    def test_it_imports_nothing_from_the_onedriveui_package(self):
        """The whole reason this test exists: nautilus-python dlopens the SYSTEM
        libpython, cannot see our package, and an import of it fails at load
        with **no useful error** — Nautilus logs nothing and the extension never
        appears. There is nothing to debug from, so it is asserted instead."""
        tree = ast.parse(ext_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("onedriveui"), \
                        f"line {node.lineno}: imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("onedriveui"), \
                    f"line {node.lineno}: imports from {node.module}"
                assert node.level == 0, \
                    f"line {node.lineno}: a relative import reaches the package"

    def test_it_imports_only_the_standard_library_and_gi(self):
        allowed = {"__future__", "json", "os", "socket", "threading", "typing",
                   "gi", "sys", "time", "logging", "errno", "stat"}
        tree = ast.parse(ext_source())
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                used.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                used.add(node.module.split(".")[0])
        assert used <= allowed, f"unexpected imports: {sorted(used - allowed)}"

    def test_the_module_parses_under_the_system_interpreter(self):
        """It runs under whatever Python Nautilus dlopened, not ours."""
        result = subprocess.run(
            [sys.executable, "-c",
             f"import ast; ast.parse(open({str(EXT_PATH)!r}).read())"],
            capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr.decode()


class TestDuplicatedConstants:
    """The extension cannot import them, so it restates them. These assert the
    two copies are equal — duplication with an enforced equality is the only
    arrangement that survives the constraint."""

    def test_the_emblem_map_matches_the_package(self):
        ext = ext_namespace()
        package = {state.value: stem for state, stem in EMBLEM_FOR_STATE.items()}
        assert ext["EMBLEM_FOR_STATE"] == package

    def test_the_status_labels_match_the_package(self):
        assert ext_namespace()["LABEL_FOR_STATE"] == FILE_STATE_LABEL

    def test_the_socket_path_matches_paths(self):
        assert ext_namespace()["SOCKET_PATH"] == str(paths.ipc_socket())

    def test_the_protocol_version_matches(self):
        from onedriveui.platform.ipc import PROTOCOL_VERSION

        assert ext_namespace()["PROTOCOL_VERSION"] == PROTOCOL_VERSION

    def test_the_timeout_matches_the_constant(self):
        from onedriveui.constants import NAUTILUS_IPC_TIMEOUT_MS

        assert ext_namespace()["TIMEOUT_S"] == NAUTILUS_IPC_TIMEOUT_MS / 1000.0

    def test_every_menu_action_is_a_real_recovery_action(self):
        """An id the server cannot dispatch is a menu item that does nothing."""
        from onedriveui.models import RecoveryAction

        known = {a.value for a in RecoveryAction} | {"pin"}
        for action, label in ext_namespace()["MENU_ITEMS"]:
            assert action in known, action
            assert label


class TestExtensionSafety:

    def test_get_background_items_never_indexes_a_possibly_empty_list(self):
        """It is called with an EMPTY list, not with the folder and not with
        None. `files[0]` there is the single most common way a Nautilus Python
        extension takes the file manager's menu down with it."""
        tree = ast.parse(ext_source())
        target = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "get_background_items")
        for node in ast.walk(target):
            if isinstance(node, ast.Subscript) and isinstance(node.slice,
                                                              ast.Constant):
                assert node.slice.value != 0, \
                    f"line {node.lineno}: indexes [0] on a possibly empty list"

    def test_the_socket_setup_is_guarded_against_the_double_import(self):
        """Nautilus imports the module twice per launch cycle — once to
        enumerate the providers and once to instantiate them."""
        source = ext_source()
        assert "_CLIENT_LOCK" in source
        assert "threading.Lock()" in source

    def test_it_never_blocks_without_a_timeout(self):
        """This runs on Nautilus's UI thread, where a blocking call is a frozen
        file manager."""
        assert "settimeout" in ext_source()


# ═════════════════════════════════════════════════════════════════════════════
# The composition root
# ═════════════════════════════════════════════════════════════════════════════

class TestStartupOrder:

    def test_the_order_is_published(self):
        assert STARTUP_ORDER[0] == "qt"
        assert STARTUP_ORDER[-2] == "supervisor"
        assert "glib_pump" in STARTUP_ORDER

    def test_qt_comes_before_the_glib_pump(self):
        """The pump is a QTimer; it needs an event loop to attach to."""
        assert STARTUP_ORDER.index("qt") < STARTUP_ORDER.index("glib_pump")

    def test_logging_comes_before_anything_that_can_fail(self):
        assert STARTUP_ORDER.index("logging") == 1

    def test_the_theme_is_installed_before_any_widget(self):
        """Qt applies a stylesheet as widgets are polished, so a window built
        before the sheet keeps Fusion's defaults for its whole life — a
        half-styled window rather than an error."""
        assert STARTUP_ORDER.index("theme") < STARTUP_ORDER.index("ui")
        # After config, because the accent source is a setting.
        assert STARTUP_ORDER.index("config") < STARTUP_ORDER.index("theme")

    def test_config_comes_before_the_database(self):
        """The database's location comes from config."""
        assert STARTUP_ORDER.index("config") < STARTUP_ORDER.index("database")

    def test_the_supervisor_is_started_last(self):
        """It ticks immediately, and a tick that reaches a half-built service is
        the class of bug that only appears on a slow machine."""
        for step in ("config", "database", "platform", "rc", "services"):
            assert STARTUP_ORDER.index(step) < STARTUP_ORDER.index("supervisor")

    def test_the_constructor_follows_it(self):
        """Asserted against the code, because an ordering constraint that lives
        only in a comment is one that gets violated within a month."""
        tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        init = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "__init__"
                    and any(isinstance(n, ast.Attribute) and n.attr == "_start_qt"
                            for n in ast.walk(node)))
        seen: list[str] = []
        for node in ast.walk(init):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_start_"):
                seen.append(node.attr[len("_start_"):])
        assert seen[:4] == ["qt", "logging", "pump", "theme"]


class TestBuildEngine:

    def test_it_builds_without_a_display_or_a_daemon(self, qapp, _isolate_home):
        """Milestone M1's shape: the whole engine, no UI at all."""
        from onedriveui import config
        from onedriveui.data.writer import DbWriter

        writer = DbWriter(paths.db_file())
        writer.start_writer()
        try:
            account = AccountInfo(id="onedrive", remote="onedrive",
                                  sync_root=str(paths.data_dir() / "OneDrive"))
            engine = build_engine(account, cfg=config.defaults(),
                                  writer=writer, headless=True)
            assert engine.supervisor is not None
            assert engine.services["notifier"] is None    # headless
            assert engine.services["ipc"] is None
        finally:
            writer.stop()

    def test_the_issue_engine_can_reach_the_supervisor(self, qapp, _isolate_home):
        """A fix offered by an issue must reach the same guards as the identical
        menu item, which means going through the same `do()`."""
        from onedriveui import config
        from onedriveui.data.writer import DbWriter

        writer = DbWriter(paths.db_file())
        writer.start_writer()
        try:
            account = AccountInfo(id="onedrive", remote="onedrive",
                                  sync_root=str(paths.data_dir() / "OneDrive"))
            engine = build_engine(account, cfg=config.defaults(),
                                  writer=writer, headless=True)
            assert engine.services["issues"]._supervisor is engine.supervisor
        finally:
            writer.stop()


class TestBringUp:

    def test_starting_the_app_brings_the_daemon_up(self, qapp, _isolate_home):
        """ARCHITECTURE §5.2: the control plane is "always up, starts before any
        account exists". Nothing else in the running application starts it, so
        without this the tray sits in ERROR forever — an honest report about a
        daemon we were supposed to have started ourselves.
        """
        from onedriveui.app import Engine

        calls: list[str] = []

        class Rcd:
            def ensure_running(self):
                calls.append("rcd")

        class Mountd:
            def ensure_mounted(self, account):
                calls.append("mount")

        engine = Engine(account=AccountInfo(id="onedrive", remote="onedrive"),
                        services={"rcd": Rcd(), "mountd": Mountd()})
        assert engine.bring_up() == []
        assert calls == ["rcd", "mount"]

    def test_a_failure_is_reported_not_raised(self, qapp, _isolate_home):
        """A dead daemon is a state the ladder already knows how to show; it is
        not a reason for the application to fail to launch."""
        from onedriveui.app import Engine

        class Broken:
            def ensure_running(self):
                raise OSError("systemd is not running")

        engine = Engine(account=AccountInfo(id="onedrive", remote="onedrive"),
                        services={"rcd": Broken()})
        problems = engine.bring_up()
        assert len(problems) == 1
        assert "control daemon" in problems[0]

    def test_the_headless_commands_do_not_install_anything(self):
        """`--state` asks what is happening. Installing a systemd unit as a side
        effect of asking a question would be a surprise."""
        source = (REPO_ROOT / "onedriveui" / "__main__.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and \
                    node.name in ("_headless_engine", "_print_state",
                                  "_print_status", "_doctor"):
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute):
                        assert child.attr != "bring_up", \
                            f"{node.name} brings services up"


class TestAccountFanOut:
    """`accounts()` is the fan-out point: one engine, one tray icon and one
    mount unit per entry it returns."""

    @staticmethod
    def _app(*entries):
        """An `Application` with a config and nothing else — `accounts()` reads
        only `self.config`, and building the real thing would want Qt, a theme
        and a database writer to answer a question about a list."""
        from onedriveui.app import Application

        app = Application.__new__(Application)
        app.config = SimpleNamespace(accounts=list(entries))
        return app

    @staticmethod
    def _entry(account_id, *, enabled=True):
        info = AccountInfo(id=account_id, remote=account_id, enabled=enabled)
        return SimpleNamespace(to_account_info=lambda: info)

    def test_a_disabled_account_gets_no_engine(self):
        """A left-over `onedrive` entry with `enabled: false` used to come back
        from here anyway, and `start()` then gave it a tray icon (a second,
        identical cloud in the bar) and a mount unit — pointed at a live remote
        the user was already mounting elsewhere. Two mounts of one account
        cannot see each other's renames; that is how a real file was lost."""
        app = self._app(self._entry("onedrive", enabled=False),
                        self._entry("onedriveui_test"))
        assert [a.id for a in app.accounts()] == ["onedriveui_test"]

    def test_enabled_accounts_all_come_back(self):
        """Two *enabled* accounts are two tray icons on purpose: that is how the
        Windows client shows a personal and a work drive."""
        app = self._app(self._entry("personal"), self._entry("work"))
        assert [a.id for a in app.accounts()] == ["personal", "work"]

    def test_an_entry_that_cannot_project_is_skipped(self):
        app = self._app(SimpleNamespace(), self._entry("personal"))
        assert [a.id for a in app.accounts()] == ["personal"]


class TestAutostartReconcile:
    """`app.autostart` is a config key; autostart is a file on disk. Something
    has to turn one into the other."""

    @staticmethod
    def _app(*, headless=False, **settings):
        from onedriveui.app import Application

        app = Application.__new__(Application)
        app.headless = headless
        app.config = SimpleNamespace(
            get=lambda key, default=None: settings.get(key, default))
        return app

    @staticmethod
    def _spy(monkeypatch, installed):
        """Replace the two `autostart` calls `_sync_autostart` makes."""
        from onedriveui.platform import autostart

        calls: list[tuple] = []
        monkeypatch.setattr(autostart, "method", lambda: installed)
        monkeypatch.setattr(
            autostart, "set_enabled",
            lambda enable, method=autostart.METHOD_SYSTEMD, **kw:
                (calls.append((enable, method)), method if enable else "none")[1])
        return calls

    def test_the_toggle_reaches_the_disk(self, monkeypatch):
        """The Settings switch writes `app.autostart` and emits
        `config_changed`; every control on that page goes through the same
        uniform `_write`. If nobody listens, the config claims autostart is on
        and neither the unit nor the XDG entry exists — which is exactly what a
        user reports as "autostart didn't work"."""
        calls = self._spy(monkeypatch, "none")
        app = self._app(**{"app.autostart": True,
                           "app.autostart_method": "systemd"})
        app._sync_autostart()
        assert calls == [(True, "systemd")]

    def test_turning_it_off_removes_what_is_installed(self, monkeypatch):
        calls = self._spy(monkeypatch, "systemd")
        app = self._app(**{"app.autostart": False})
        app._sync_autostart()
        assert calls == [(False, "systemd")]

    def test_an_agreeing_disk_is_left_alone(self, monkeypatch):
        """Rewriting a unit that is already correct would restart it at every
        launch."""
        calls = self._spy(monkeypatch, "systemd")
        app = self._app(**{"app.autostart": True,
                           "app.autostart_method": "systemd"})
        app._sync_autostart()
        assert calls == []

    def test_a_method_change_migrates(self, monkeypatch):
        calls = self._spy(monkeypatch, "systemd")
        app = self._app(**{"app.autostart": True,
                           "app.autostart_method": "xdg"})
        app._sync_autostart()
        assert calls == [(True, "xdg")]

    def test_headless_installs_nothing(self, monkeypatch):
        """`--state` asks a question. It does not opt the user into a login
        unit as a side effect of answering one."""
        calls = self._spy(monkeypatch, "none")
        app = self._app(headless=True, **{"app.autostart": True})
        app._sync_autostart()
        assert calls == []

    def test_a_failure_does_not_stop_the_client(self, monkeypatch):
        """A missing autostart entry is a missing convenience. Refusing to
        launch over it would trade that for a missing client."""
        from onedriveui.platform import autostart

        monkeypatch.setattr(autostart, "method", lambda: "none")

        def boom(*a, **kw):
            raise OSError("systemd is not running")

        monkeypatch.setattr(autostart, "set_enabled", boom)
        app = self._app(**{"app.autostart": True})
        app._sync_autostart()          # must not raise

    def test_start_reconciles_before_it_fans_out(self):
        """On start-up too, not only on the toggle: a config restored from a
        backup, or written by a script, says `true` over a disk that holds
        neither mechanism, and only a reconcile at launch fixes it."""
        source = (REPO_ROOT / "onedriveui" / "app.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        # `Application.start`, not the local helper of the same name inside
        # `build_engine`, which `ast.walk` reaches first.
        start = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "start"
                     and [a.arg for a in n.args.args] == ["self"])
        called = [c.func.attr for c in ast.walk(start)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)]
        assert "_sync_autostart" in called


class TestActivityCenterWiring:

    def test_its_signals_are_connected(self, qapp, _isolate_home):
        """The window declares `settings_requested` and `help_requested`; only
        the composition root knows what a window is. Unconnected, the header
        gear and the footer buttons are dead controls that look identical to
        working ones — which is exactly how this was found.
        """
        tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
        target = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "open_activity")

        connected: set[str] = set()
        for node in ast.walk(target):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect"):
                continue
            receiver = node.func.value
            if isinstance(receiver, ast.Attribute):
                connected.add(receiver.attr)

        assert "settings_requested" in connected
        assert "help_requested" in connected

    def test_the_window_declares_what_the_root_connects(self, qapp):
        """A rename on either side has to fail here rather than in a click."""
        from onedriveui.ui.activity_center import ActivityCenter

        for name in ("settings_requested", "help_requested"):
            assert hasattr(ActivityCenter, name), name


class TestSystemdAdapter:

    def test_it_satisfies_the_protocol_the_rc_layer_needs(self):
        """WP-02 depends on the *shape* of a service manager, which is what let
        it and the platform layer be built independently."""
        for name in ("write_unit", "daemon_reload", "enable", "start", "stop",
                     "restart", "is_active", "status_text"):
            assert callable(getattr(SystemdAdapter, name)), name


# ═════════════════════════════════════════════════════════════════════════════
# The CLI
# ═════════════════════════════════════════════════════════════════════════════

def run_cli(*args: str, home: pathlib.Path) -> subprocess.CompletedProcess:
    """Run the real CLI in a subprocess with an isolated home."""
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "QT_QPA_PLATFORM": "offscreen",
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT),
    }
    (home / "run").mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, "-m", "onedriveui", *args],
                          capture_output=True, text=True, timeout=180,
                          cwd=str(REPO_ROOT), env=env)


@pytest.fixture
def configured_home(tmp_path) -> pathlib.Path:
    """An isolated home with a valid `config.json`."""
    from tests.conftest import default_config

    home = tmp_path / "home"
    (home / ".config" / "onedriveui").mkdir(parents=True)
    (home / "OneDrive").mkdir(parents=True)
    cfg = default_config()
    cfg["accounts"][0]["sync_root"] = str(home / "OneDrive")
    (home / ".config" / "onedriveui" / "config.json").write_text(
        json.dumps(cfg, indent=2), encoding="utf-8")
    return home


@pytest.mark.slow
class TestCli:

    def test_version(self, tmp_path):
        result = run_cli("--version", home=tmp_path)
        assert result.returncode == 0
        assert result.stdout.strip() == f"onedriveui {__version__}"

    def test_state_prints_one_word_on_stdout(self, configured_home):
        """Milestone M1: the whole engine, one SyncState, no GUI.

        stdout carries the answer and nothing else, so a script can read it.
        """
        result = run_cli("--state", home=configured_home)
        printed = result.stdout.strip()
        assert printed in {s.value for s in SyncState}, result.stderr[-2000:]
        assert "\n" not in printed

    def test_state_without_an_account_says_signed_out(self, tmp_path):
        home = tmp_path / "empty"
        home.mkdir()
        result = run_cli("--state", home=home)
        assert result.stdout.strip() == "signed_out"
        assert result.returncode == 1

    def test_status_is_valid_json(self, configured_home):
        result = run_cli("--status", home=configured_home)
        payload = json.loads(result.stdout)
        assert payload["state"] in {s.value for s in SyncState}
        assert payload["rung"]
        assert "stale_sources" in payload

    def test_status_answers_about_the_active_account(self, configured_home):
        """With two accounts configured, `--status` must name the ACTIVE one.

        It used to take `accounts[0]` — whichever happened to be first in the
        file. The symptom is a snapshot that reports a different account than
        the tray is showing, which is how a live test rig was read as healthy
        while the account under test had never been looked at.
        """
        path = configured_home / ".config" / "onedriveui" / "config.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        second = json.loads(json.dumps(cfg["accounts"][0]))
        second["id"] = "second"
        second["remote"] = "second"
        second["sync_root"] = str(configured_home / "Second")
        (configured_home / "Second").mkdir()
        cfg["accounts"].append(second)
        cfg["app"]["active_account_id"] = "second"
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        payload = json.loads(run_cli("--status", home=configured_home).stdout)
        assert payload["account"] == "second"

    def test_the_status_headline_comes_from_the_reducer(self, configured_home):
        """Not a second wording invented for the command line: the same
        `status_text()` the tray tooltip uses."""
        from onedriveui.strings import STATUS_LINE

        payload = json.loads(run_cli("--status", home=configured_home).stdout)
        templates = {t.split("{")[0].strip() for t in STATUS_LINE.values()}
        assert any(payload["headline"].startswith(t) for t in templates if t)

    def test_doctor_reports_each_check(self, configured_home):
        result = run_cli("--doctor", home=configured_home)
        assert "account configured" in result.stdout
        assert "Nautilus extension installed" in result.stdout
        assert "icons installed in hicolor" in result.stdout

    def test_installing_the_extension_writes_it_and_the_icons(self,
                                                              configured_home):
        result = run_cli("--install-extension", home=configured_home)
        assert result.returncode == 0, result.stderr[-2000:]
        installed = (configured_home / ".local/share/nautilus-python"
                     / "extensions" / "nautilus_onedriveui.py")
        assert installed.exists()
        # The step that is invisible when skipped: without the SVGs in hicolor
        # every emblem silently fails to appear under the user's own theme.
        svgs = list((configured_home / ".local/share/icons/hicolor").rglob("*.svg"))
        assert len(svgs) >= 20

    def test_the_restart_hint_is_always_printed(self, configured_home):
        """Nautilus does not hot-reload; without `nautilus -q` nothing changes,
        and "I installed it and nothing happened" is the result."""
        result = run_cli("--install-extension", home=configured_home)
        assert "nautilus -q" in result.stdout

    def test_doctor_passes_the_install_checks_after_installing(self,
                                                               configured_home):
        run_cli("--install-extension", home=configured_home)
        result = run_cli("--doctor", home=configured_home)
        lines = {line.split("] ", 1)[1].split(" —")[0]: line.startswith("[ok")
                 for line in result.stdout.splitlines() if "] " in line}
        assert lines["Nautilus extension installed"] is True


# ═════════════════════════════════════════════════════════════════════════════
# Packaging
# ═════════════════════════════════════════════════════════════════════════════

class TestPackaging:

    def test_the_backend_is_setuptools(self):
        """hatchling is not installed and naming it would turn every build into
        a network fetch."""
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        assert data["build-system"]["build-backend"] == "setuptools.build_meta"
        assert not any("hatch" in r for r in data["build-system"]["requires"])

    def test_pyside6_is_never_a_dependency(self):
        """A PyPI wheel ships its own Qt and SHADOWS the system build. The
        symptoms are not import errors — they are a tray icon that registers no
        StatusNotifierItem, which reads as an application bug."""
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        declared = " ".join(data["project"]["dependencies"]).lower()
        for group in data["project"].get("optional-dependencies", {}).values():
            declared += " " + " ".join(group).lower()
        assert "pyside" not in declared
        assert "pygobject" not in declared

    def test_the_schema_ships_with_the_package(self):
        """A wheel without `schema.sql` installs an application that cannot open
        its own database."""
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        package_data = data["tool"]["setuptools"]["package-data"]
        assert "schema.sql" in package_data["onedriveui.data"]

    def test_the_console_script_points_at_main(self):
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        assert data["project"]["scripts"]["onedriveui"] == \
            "onedriveui.__main__:main"

    def test_pytest_is_configured_in_exactly_one_place(self):
        """Both would be legal and `pytest.ini` wins, so a section in
        `pyproject.toml` would be dead configuration that silently drifts."""
        import tomllib

        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
        assert "pytest" not in data.get("tool", {})
        assert (REPO_ROOT / "pytest.ini").exists()

    @pytest.mark.parametrize("name", [
        "onedriveui.service.in", "onedriveui-rcd.service.in",
        "onedriveui-mount@.service.in", "onedriveui.desktop.in",
    ])
    def test_the_template_exists(self, name):
        assert (PACKAGING / name).exists()

    def test_no_unit_names_network_online_target(self):
        """It DOES NOT EXIST in the --user manager: `After=` and `Wants=` on it
        are silently ignored, so a unit that names it orders nothing while
        looking correct."""
        for path in PACKAGING.glob("*.service.in"):
            for line in path.read_text(encoding="utf-8").splitlines():
                # Directives only. The comment explaining why the target must
                # not be used is the reason it stays unused.
                if line.strip().startswith("#"):
                    continue
                assert "network-online.target" not in line, f"{path.name}: {line}"

    def test_the_mount_unit_carries_no_backend_flag(self):
        """Invariant I1. One `--onedrive-*` flag renames the filesystem to
        `onedrive{HASH}:`, stranding the whole VFS cache — which has already
        happened twice on this machine."""
        text = (PACKAGING / "onedriveui-mount@.service.in").read_text(
            encoding="utf-8")
        for line in text.splitlines():
            if line.strip().startswith("#"):
                continue
            assert "--onedrive-" not in line, line

    def test_the_mount_unit_unmounts_lazily(self):
        """A mount with an open handle cannot be unmounted otherwise, and
        systemd would sit through its whole timeout before killing rclone with
        the mount still in the table."""
        text = (PACKAGING / "onedriveui-mount@.service.in").read_text(
            encoding="utf-8")
        assert "fusermount3 -uz" in text

    def test_no_unit_binds_a_forbidden_port(self):
        """5572 and 5573 are already occupied on this machine by the user's own
        rclone; 53682 is OAuth's fixed callback port."""
        from onedriveui.constants import RC_FORBIDDEN_PORTS

        for path in PACKAGING.glob("*.in"):
            text = path.read_text(encoding="utf-8")
            for port in RC_FORBIDDEN_PORTS:
                for line in text.splitlines():
                    if line.strip().startswith("#"):
                        continue
                    assert str(port) not in line, f"{path.name}: {line}"

    def test_the_desktop_entry_registers_the_odopen_scheme(self):
        """Without it a browser's "Open in OneDrive" link offers nothing and
        appears broken."""
        text = (PACKAGING / "onedriveui.desktop.in").read_text(encoding="utf-8")
        assert "x-scheme-handler/odopen" in text

    def test_the_autostart_unit_runs_a_command_the_cli_accepts(self):
        """The unit and the CLI are written in different packages and nothing
        checks that they agree — `systemd-analyze verify` does not run the
        command. When they disagree, argparse exits 2, systemd retries five
        times and gives up, and the client silently never starts at login.
        """
        from onedriveui.__main__ import build_parser
        from onedriveui.platform import autostart

        flags = {opt for action in build_parser()._actions
                 for opt in action.option_strings}
        for text in (autostart.gui_unit_text(), autostart.autostart_entry_text()):
            for line in text.splitlines():
                if not line.startswith(("ExecStart=", "Exec=")):
                    continue
                for word in line.split():
                    if word.startswith("--"):
                        assert word in flags, f"{word} is not a CLI option"

    def test_every_desktop_action_is_a_real_cli_flag(self):
        """`desktop-file-validate` does not check this, so a stale action is a
        menu item that silently does nothing."""
        from onedriveui.__main__ import build_parser

        text = (PACKAGING / "onedriveui.desktop.in").read_text(encoding="utf-8")
        flags = {opt for action in build_parser()._actions
                 for opt in action.option_strings}
        for line in text.splitlines():
            if not line.startswith("Exec=") or "--" not in line:
                continue
            flag = "--" + line.split("--", 1)[1].split()[0]
            assert flag in flags, f"{flag} is not a CLI option"
