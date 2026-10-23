"""The frozen binary must find a CA store on distributions it was not built on.

The Linux binary is built in Debian bookworm so it links a glibc old enough to
run on Ubuntu 22.04 and RHEL 9. That bakes Debian's OpenSSL default CA paths
(/usr/lib/ssl/...) into the bundle. Those paths do not exist on Fedora, RHEL,
Rocky, Alma, openSUSE or Arch, where the default SSL context then loads zero
certificates and every connection to Discord fails with
CERTIFICATE_VERIFY_FAILED. This points OpenSSL at a working store when, and
only when, the platform's own one is missing.
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
    # ensure_ca_bundle writes os.environ directly. setenv first so teardown
    # restores the real starting value, not a fake store a test left behind
    # for every later TLS client in the run.
    monkeypatch.setenv("SSL_CERT_FILE", "placeholder")
    monkeypatch.delenv("SSL_CERT_FILE")


def _platform(monkeypatch, *, frozen=True, ca_count):
    monkeypatch.setattr("rangecontrol.main.is_frozen", lambda: frozen)
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: FakeContext(ca_count))


def test_a_missing_platform_store_is_replaced_by_the_best_one_available(monkeypatch, tmp_path):
    """The Fedora case. The OS store keeps getting new roots, so the first one
    present wins; the copy inside the binary ages from day one and is only a
    last resort; and having neither must not stop startup."""
    present = tmp_path / "real.crt"
    present.write_text("x", encoding="utf-8")
    _platform(monkeypatch, ca_count=0)

    monkeypatch.setattr(
        "rangecontrol.main._SYSTEM_CA_FILES",
        ("/nonexistent/a.crt", str(present), "/nonexistent/b.crt"),
    )
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == str(present)

    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setattr("rangecontrol.main._SYSTEM_CA_FILES", ())
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")
    assert os.path.exists(os.environ["SSL_CERT_FILE"])

    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setattr("rangecontrol.main._bundled_ca_file", lambda: None)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ


def test_working_stores_and_operator_choices_are_left_alone(monkeypatch):
    """Debian, Ubuntu and Windows already load a store (overriding Windows
    would discard an enterprise root); from source the interpreter's store is
    correct by construction; and an operator's corporate CA always wins."""
    _platform(monkeypatch, ca_count=60)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ

    _platform(monkeypatch, frozen=False, ca_count=0)
    ensure_ca_bundle()
    assert "SSL_CERT_FILE" not in os.environ

    monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
    _platform(monkeypatch, ca_count=0)
    ensure_ca_bundle()
    assert os.environ["SSL_CERT_FILE"] == "/etc/corp/ca.pem"


def test_main_applies_it_before_anything_else(monkeypatch):
    """It must run before any SSL context exists, i.e. before the GUI or bot."""
    calls = []
    monkeypatch.setattr("rangecontrol.main.ensure_ca_bundle", lambda: calls.append("ca"))
    monkeypatch.setattr("rangecontrol.gui.app.run_gui", lambda: calls.append("gui") or 0)
    from rangecontrol.main import main

    assert main([]) == 0
    assert calls == ["ca", "gui"]
