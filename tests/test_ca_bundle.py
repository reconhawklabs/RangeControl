"""The frozen binary must find a CA store on distributions it was not built on.

The Linux binary is built in Debian bookworm so it links a glibc old enough to
run on Ubuntu 22.04 and RHEL 9. That bakes Debian's OpenSSL default CA paths
(/usr/lib/ssl/...) into the bundle. Those paths do not exist on Fedora, RHEL,
Rocky, Alma, openSUSE or Arch, where the default SSL context then loads zero
certificates and every connection to Discord fails with
CERTIFICATE_VERIFY_FAILED. certifi's bundle ships inside the binary; this
points OpenSSL at it when, and only when, the platform's own store is missing.
"""

import os
import ssl

import pytest

from rangecontrol.main import ensure_ca_bundle


class FakeContext:
    """Stands in for ssl.create_default_context()'s result."""

    def __init__(self, ca_count):
        self._ca_count = ca_count

    def get_ca_certs(self):
        return [{"subject": ()}] * self._ca_count


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


def _freeze(monkeypatch, *, ca_count):
    monkeypatch.setattr("rangecontrol.main.is_frozen", lambda: True)
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: FakeContext(ca_count))


def test_a_missing_platform_store_falls_back_to_the_bundled_one(monkeypatch):
    """This is the Fedora case: Debian's paths simply are not there."""
    _freeze(monkeypatch, ca_count=0)
    monkeypatch.setattr("rangecontrol.main._SYSTEM_CA_FILES", ())
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")
    assert os.path.exists(os.environ["SSL_CERT_FILE"])


def test_a_working_platform_store_is_left_alone(monkeypatch):
    """Debian and Ubuntu already work; do not second-guess them."""
    _freeze(monkeypatch, ca_count=140)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ


def test_the_windows_store_is_not_overridden(monkeypatch):
    """Windows loads its own store even though the compiled paths are absent.

    Overriding it with certifi would discard an enterprise root and break the
    very environments most likely to have one.
    """
    _freeze(monkeypatch, ca_count=60)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ


def test_an_operator_supplied_value_always_wins(monkeypatch):
    """Someone pointing at a corporate CA must not be overridden."""
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
    _freeze(monkeypatch, ca_count=0)
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == "/etc/corp/ca.pem"


def test_it_does_nothing_when_not_frozen(monkeypatch):
    """From source, the interpreter's own store is correct by construction."""
    monkeypatch.setattr("rangecontrol.main.is_frozen", lambda: False)
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: FakeContext(0))
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ


def test_a_missing_certifi_bundle_is_not_fatal(monkeypatch):
    """Better to fail later with a TLS error than to refuse to start."""
    _freeze(monkeypatch, ca_count=0)
    monkeypatch.setattr("rangecontrol.main._SYSTEM_CA_FILES", ())
    monkeypatch.setattr("rangecontrol.main._bundled_ca_file", lambda: None)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ


def test_main_calls_it_before_anything_else(monkeypatch):
    """It must run before any SSL context exists, i.e. before the GUI or bot."""
    calls = []
    monkeypatch.setattr("rangecontrol.main.ensure_ca_bundle",
                        lambda: calls.append("ca"))
    monkeypatch.setattr("rangecontrol.gui.app.run_gui",
                        lambda: calls.append("gui") or 0)
    from rangecontrol.main import main

    main([])
    assert calls == ["ca", "gui"]


def test_the_system_store_is_preferred_over_the_bundled_one(monkeypatch, tmp_path):
    """The OS store keeps getting new roots; the bundled copy ages from day one."""
    system = tmp_path / "ca-certificates.crt"
    system.write_text("x", encoding="utf-8")
    _freeze(monkeypatch, ca_count=0)
    monkeypatch.setattr("rangecontrol.main._SYSTEM_CA_FILES", (str(system),))
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == str(system)


def test_the_bundle_is_only_a_last_resort(monkeypatch):
    """With no system store anywhere, the frozen copy is better than nothing."""
    _freeze(monkeypatch, ca_count=0)
    monkeypatch.setattr("rangecontrol.main._SYSTEM_CA_FILES", ())
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")


def test_probing_stops_at_the_first_store_that_exists(monkeypatch, tmp_path):
    present = tmp_path / "real.crt"
    present.write_text("x", encoding="utf-8")
    _freeze(monkeypatch, ca_count=0)
    monkeypatch.setattr(
        "rangecontrol.main._SYSTEM_CA_FILES",
        ("/nonexistent/a.crt", str(present), "/nonexistent/b.crt"),
    )
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == str(present)
