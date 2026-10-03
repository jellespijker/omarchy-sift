"""Every test runs against a private config and state directory, never the real ones.

Tests that need a filesystem with user extended attributes carry `@pytest.mark.xattr`. They are skipped where the temp folder cannot hold
xattrs (some container and CI filesystems). `SIFT_TEST_NO_XATTR=1` simulates such a filesystem, so the rest of the suite is proven to
work without them (the CI runs both ways)."""
import errno
import os
import tempfile

import pytest


def _real_xattr_ok() -> bool:
    try:
        with tempfile.TemporaryFile() as f:
            os.setxattr(f.fileno(), "user.sift_probe", b"1")
        return True
    except OSError:
        return False


if os.environ.get("SIFT_TEST_NO_XATTR"):
    def _refuse(*a, **k):
        raise OSError(errno.ENOTSUP, "Operation not supported")
    os.setxattr = _refuse                      # type: ignore[assignment]
    XATTR_OK = False
else:
    XATTR_OK = _real_xattr_ok()


def pytest_configure(config):
    config.addinivalue_line("markers", "xattr: needs a filesystem that supports user extended attributes")


def pytest_runtest_setup(item):
    if item.get_closest_marker("xattr") and not XATTR_OK:
        pytest.skip("this filesystem has no user extended attributes")


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sift-user")
    monkeypatch.setenv("SIFT_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("SIFT_STATE_DIR", str(base / "state"))
    monkeypatch.setenv("SIFT_SYSTEMD_DIR", str(base / "systemd"))
    monkeypatch.delenv("SIFT_ENDPOINT", raising=False)
    monkeypatch.setenv("SIFT_LANG", "en")
    monkeypatch.setenv("SIFT_DEMO", "0")                 # a demo marker in a developer's real config must not leak into tests
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "C.UTF-8")
    from sift import i18n
    i18n._current = None


@pytest.fixture
def xattr_required():
    """Request this from a fixture that writes xattrs: the tests using it are skipped on filesystems without them."""
    if not XATTR_OK:
        pytest.skip("this filesystem has no user extended attributes")
