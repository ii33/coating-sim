from __future__ import annotations

import json

import pytest

from coat_sim.ui import assistant_credentials, settings


def test_compute_server_settings_default_and_legacy_migration():
    defaults = settings.normalized_ui_settings()
    assert defaults["compute_server_url"] == ""
    assert defaults["compute_server_tls"] is True
    assert defaults["compute_server_ca_file"] == ""

    migrated = settings.normalized_ui_settings({
        "compute_server_url": " https://compute.example:8443/api/ ",
        "compute_server_tls": False,
        "compute_server_ca_file": "  /etc/coat/ca.pem  ",
    })
    assert migrated["compute_server_url"] == "https://compute.example:8443/api"
    assert migrated["compute_server_tls"] is False
    assert migrated["compute_server_ca_file"] == "/etc/coat/ca.pem"


@pytest.mark.parametrize("url", [
    "https://name:secret@compute.example/api",
    "https://compute.example/api?token=hidden",
    "https://compute.example/api#fragment",
    "file:///tmp/compute",
    "https://compute.example:invalid",
])
def test_invalid_compute_server_url_is_dropped(url):
    assert settings.normalized_ui_settings({"compute_server_url": url})["compute_server_url"] == ""


def test_compute_server_settings_round_trip_excludes_token(tmp_path, monkeypatch):
    path = tmp_path / "ui_settings.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", path)
    values = settings.save_ui_settings({
        "compute_server_url": "https://compute.example/api",
        "compute_server_tls": True,
        "compute_server_ca_file": "/certs/internal.pem",
    })
    assert settings.load_ui_settings()["compute_server_url"] == values["compute_server_url"]
    assert settings.load_ui_settings()["compute_server_tls"] is True
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "compute_server_token" not in persisted
    assert "opaque-example-token" not in path.read_text(encoding="utf-8")


class _MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        del self.values[(service, account)]


def test_compute_token_uses_endpoint_bound_keyring_only(monkeypatch):
    backend = _MemoryKeyring()
    monkeypatch.setattr(assistant_credentials, "_backend", lambda: backend)

    assistant_credentials.save_compute_token("opaque-example-token", "https://compute.example/api/")
    assert assistant_credentials.read_compute_token("https://compute.example/api") == "opaque-example-token"
    assert assistant_credentials.read_compute_token("https://other.example/api") is None
    assert len(backend.values) == 1
    assert next(iter(backend.values))[0] == assistant_credentials.COMPUTE_SERVICE

    assistant_credentials.delete_compute_token("https://compute.example/api")
    assert assistant_credentials.read_compute_token("https://compute.example/api") is None


@pytest.mark.parametrize("secret", ["", "has whitespace", "line\nbreak", "non-ascii-令牌"])
def test_compute_token_rejects_invalid_values(monkeypatch, secret):
    monkeypatch.setattr(assistant_credentials, "_backend", lambda: _MemoryKeyring())
    with pytest.raises(ValueError):
        assistant_credentials.save_compute_token(secret, "https://compute.example")


def test_settings_ui_exposes_explicit_connection_controls():
    qml = (settings.PROJECT_ROOT / "coat_sim" / "ui" / "qml" / "SettingsWindow.qml").read_text(encoding="utf-8")
    assert 'objectName: "computeServerSettingsCategory"' in qml
    assert 'objectName: "computeServerUrlField"' in qml
    assert 'objectName: "computeServerTlsCheck"' in qml
    assert 'objectName: "computeServerCaField"' in qml
    assert 'objectName: "saveComputeServerTokenButton"' in qml
    assert "settingsContent.settingsBridge.refreshComputeServerTokenStatus()" in qml
    assert "连接状态尚未检查" in qml
