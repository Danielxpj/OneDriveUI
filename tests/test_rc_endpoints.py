"""WP-02 — `onedriveui/rc/endpoints.py`.

Ports are probed by really binding them, so the tests do too: a socket bound on
loopback inside the test is the only honest way to prove the probe notices.
Nothing here touches the network, rclone, or a port outside
`constants.RC_PORT_RANGE` except to prove the forbidden ones are skipped.
"""

from __future__ import annotations

import json
import os
import socket
import stat

import pytest

from onedriveui import APP_ID, paths
from onedriveui.constants import RC_FORBIDDEN_PORTS, RC_PORT_RANGE
from onedriveui.errors import OneDriveUIError
from onedriveui.models import RcEndpoint
from onedriveui.rc import endpoints


@pytest.fixture
def bound_port():
    """Bind a real loopback port for the duration of one test and yield it."""
    held: list[socket.socket] = []

    def bind(port: int | None = None) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", port or 0))
        sock.listen(1)
        held.append(sock)
        return sock.getsockname()[1]

    try:
        yield bind
    finally:
        for sock in held:
            sock.close()


# ═════════════════════════════════════════════════════════════════════════════
# Port probing
# ═════════════════════════════════════════════════════════════════════════════

class TestPortProbe:
    def test_a_bound_port_is_not_free(self, bound_port):
        port = bound_port()
        assert endpoints.port_is_free(port) is False

    def test_an_unbound_port_is_free(self, bound_port):
        port = bound_port()          # take one...
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
        sock.close()                 # ...and release another
        assert free != port
        assert endpoints.port_is_free(free) is True

    @pytest.mark.parametrize("port", sorted(RC_FORBIDDEN_PORTS))
    def test_a_forbidden_port_is_never_reported_free(self, port):
        """5572 and 5573 belong to the user's own rclone; 53682 is rclone's fixed
        OAuth callback port. None is ever bindable by us, whatever the kernel
        says."""
        assert endpoints.port_is_free(port) is False

    def test_pick_free_port_stays_in_the_configured_range(self):
        assert endpoints.pick_free_port() in RC_PORT_RANGE

    def test_pick_free_port_never_returns_5572_5573_or_53682(self):
        """Acceptance: even asked to consider them, it skips all three."""
        for _ in range(20):
            port = endpoints.pick_free_port(ports=range(5570, 5580))
            assert port not in RC_FORBIDDEN_PORTS
        assert endpoints.pick_free_port(ports=[53682, 53683]) == 53683
        with pytest.raises(OneDriveUIError):
            endpoints.pick_free_port(ports=sorted(RC_FORBIDDEN_PORTS))

    def test_pick_free_port_skips_a_port_that_is_taken(self, bound_port):
        """Both candidates are chosen at run time: hard-coding a port number
        would make this test depend on what the rest of the suite is binding."""
        taken = endpoints.pick_free_port()
        other = endpoints.pick_free_port(exclude=[taken])
        bound_port(taken)
        assert endpoints.pick_free_port(ports=[taken, other]) == other

    def test_pick_free_port_honours_exclude(self):
        first = endpoints.pick_free_port()
        second = endpoints.pick_free_port(exclude=[first])
        assert second != first

    def test_pick_free_port_raises_when_everything_is_busy(self, bound_port):
        window: list[int] = []
        for _ in range(3):
            port = endpoints.pick_free_port(exclude=window)
            bound_port(port)
            window.append(port)
        with pytest.raises(OneDriveUIError) as excinfo:
            endpoints.pick_free_port(ports=window)
        assert "no free rc port" in str(excinfo.value)

    def test_probing_does_not_leave_the_port_bound(self):
        port = endpoints.pick_free_port()
        assert endpoints.port_is_free(port) is True     # still free a moment later


# ═════════════════════════════════════════════════════════════════════════════
# Credentials
# ═════════════════════════════════════════════════════════════════════════════

class TestCredentials:
    def test_the_user_is_the_application_id(self):
        user, _password = endpoints.generate_credentials()
        assert user == APP_ID == endpoints.RC_USER

    def test_the_password_is_token_urlsafe_32(self):
        """32 random bytes, URL-safe base64: 43 characters, no padding, and
        nothing that needs quoting in a unit file, a ps line or an HTTP header."""
        _user, password = endpoints.generate_credentials()
        assert len(password) == 43
        assert set(password) <= set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

    def test_every_launch_gets_a_different_password(self):
        passwords = {endpoints.generate_credentials()[1] for _ in range(50)}
        assert len(passwords) == 50


# ═════════════════════════════════════════════════════════════════════════════
# endpoints.json
# ═════════════════════════════════════════════════════════════════════════════

def _rcd(port=17800, **kw):
    base = dict(kind="rcd", host="127.0.0.1", port=port, user="onedriveui",
                password="s3cret", pid=4242, starttime=99887766,
                execute_id="b16db48a-a4b6-4439-ab02-e8b6d66c7022")
    base.update(kw)
    return RcEndpoint(**base)


def _mount(account_id="onedrive", port=17801, **kw):
    base = dict(kind="mount", host="127.0.0.1", port=port, user="onedriveui",
                password="s3cret2", mountpoint="/home/u/OneDrive",
                account_id=account_id)
    base.update(kw)
    return RcEndpoint(**base)


class TestEndpointsFile:
    def test_a_missing_file_loads_as_empty(self):
        assert endpoints.load_endpoints() == {}

    def test_the_rcd_endpoint_round_trips(self):
        endpoints.save_endpoint(_rcd())
        loaded = endpoints.load_endpoints()
        assert set(loaded) == {"rcd"}
        assert loaded["rcd"] == _rcd()

    def test_a_mount_endpoint_is_keyed_by_account(self):
        endpoints.save_endpoint(_mount("onedrive"))
        endpoints.save_endpoint(_mount("work", port=17802))
        loaded = endpoints.load_endpoints()
        assert set(loaded) == {"mount:onedrive", "mount:work"}
        assert loaded["mount:work"].port == 17802

    def test_the_rcd_and_the_mounts_coexist(self):
        endpoints.save_endpoint(_rcd())
        endpoints.save_endpoint(_mount())
        assert set(endpoints.load_endpoints()) == {"rcd", "mount:onedrive"}

    def test_saving_the_same_kind_twice_replaces_rather_than_appends(self):
        endpoints.save_endpoint(_rcd(port=17800))
        endpoints.save_endpoint(_rcd(port=17850))
        loaded = endpoints.load_endpoints()
        assert len(loaded) == 1
        assert loaded["rcd"].port == 17850

    def test_the_file_is_written_0600(self):
        """It holds the rc password, and rc access is shell access."""
        path = endpoints.save_endpoint(_rcd())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_runtime_directory_is_0700(self):
        endpoints.save_endpoint(_rcd())
        assert stat.S_IMODE(paths.runtime_dir().stat().st_mode) == 0o700

    def test_the_write_is_atomic_and_leaves_no_temporary_behind(self):
        endpoints.save_endpoint(_rcd())
        strays = [p.name for p in paths.runtime_dir().iterdir()
                  if p.name != "endpoints.json"]
        assert strays == []

    def test_a_truncated_file_loads_as_empty_rather_than_raising(self):
        endpoints.save_endpoint(_rcd())
        paths.endpoints_file().write_text('{"version": 1, "rcd"', encoding="utf-8")
        assert endpoints.load_endpoints() == {}

    def test_a_foreign_schema_version_is_ignored(self):
        paths.endpoints_file().write_text(
            json.dumps({"version": 99, "rcd": {"port": 17800}}), encoding="utf-8")
        assert endpoints.load_endpoints() == {}

    def test_a_junk_entry_is_dropped_without_taking_the_others_with_it(self):
        endpoints.save_endpoint(_rcd())
        doc = json.loads(paths.endpoints_file().read_text(encoding="utf-8"))
        doc["mount"] = {"broken": "not a dict", "onedrive": {"port": 17801,
                                                             "account_id": "onedrive"}}
        paths.endpoints_file().write_text(json.dumps(doc), encoding="utf-8")
        loaded = endpoints.load_endpoints()
        assert set(loaded) == {"rcd", "mount:onedrive"}

    def test_an_endpoint_without_a_port_is_not_loaded(self):
        endpoints.save_endpoint(_rcd(port=0))
        assert endpoints.load_endpoints() == {}

    def test_forget_removes_only_the_named_endpoint(self):
        endpoints.save_endpoint(_rcd())
        endpoints.save_endpoint(_mount())
        endpoints.forget_endpoint("mount", "onedrive")
        assert set(endpoints.load_endpoints()) == {"rcd"}

    def test_forgetting_something_that_was_never_recorded_is_a_no_op(self):
        endpoints.save_endpoint(_rcd())
        endpoints.forget_endpoint("mount", "nonexistent")
        assert set(endpoints.load_endpoints()) == {"rcd"}

    def test_clear_removes_everything(self):
        endpoints.save_endpoint(_rcd())
        endpoints.save_endpoint(_mount())
        endpoints.clear_endpoints()
        assert endpoints.load_endpoints() == {}

    def test_a_mount_without_an_account_id_is_refused(self):
        with pytest.raises(ValueError, match="account_id"):
            endpoints.save_endpoint(_mount(account_id=""))

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="rcd.*mount"):
            endpoints.save_endpoint(_rcd(kind="bisync"))

    def test_the_password_is_the_only_secret_in_the_file(self):
        """A reminder in test form: this file is why `assert_bundle_safe` refuses
        to put endpoints.json in a diagnostics archive."""
        endpoints.save_endpoint(_rcd())
        assert "s3cret" in paths.endpoints_file().read_text(encoding="utf-8")


class TestIdentityHelpers:
    def test_with_identity_returns_a_new_frozen_endpoint(self):
        original = _rcd(pid=0, starttime=0, execute_id="")
        stamped = endpoints.with_identity(original, pid=99, starttime=12345,
                                          execute_id="uuid-1")
        assert original.pid == 0 and original.execute_id == ""
        assert (stamped.pid, stamped.starttime, stamped.execute_id) == (
            99, 12345, "uuid-1")
        assert stamped.port == original.port

    def test_endpoint_key_shapes(self):
        assert endpoints.endpoint_key("rcd") == "rcd"
        assert endpoints.endpoint_key("rcd", "ignored") == "rcd"
        assert endpoints.endpoint_key("mount", "onedrive") == "mount:onedrive"

    def test_known_ports_collects_disk_and_memory(self):
        endpoints.save_endpoint(_rcd(port=17800))
        assert endpoints.known_ports() == {17800}
        assert endpoints.known_ports([_mount(port=17801)]) == {17800, 17801}

    def test_pick_free_port_can_be_fed_known_ports_to_avoid_a_self_collision(self):
        """The rcd picks first; the mount must not be handed the same number
        while the rcd is still binding it."""
        endpoints.save_endpoint(_rcd(port=RC_PORT_RANGE.start))
        port = endpoints.pick_free_port(exclude=endpoints.known_ports())
        assert port != RC_PORT_RANGE.start


class TestIsolation:
    def test_the_file_lands_under_the_isolated_runtime_dir(self, _isolate_home):
        path = endpoints.save_endpoint(_rcd())
        assert str(path).startswith(str(_isolate_home))
        assert path.name == "endpoints.json"
        assert os.path.samefile(path.parent, paths.runtime_dir())
