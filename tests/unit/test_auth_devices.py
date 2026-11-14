"""Unit tests for :class:`lancast.auth.DeviceStore`."""

from __future__ import annotations

import json

import pytest

from lancast.auth import DeviceStore


def test_issue_verify_and_token_is_never_stored_in_plaintext(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path)

    device, token = store.issue("iPad", "192.168.1.20", "pytest-UA")

    assert device.device_id.startswith("d_")
    assert len(token) >= 40
    assert store.verify(device.device_id, token) is not None
    assert store.verify(device.device_id, token).name == "iPad"

    raw = path.read_text(encoding="utf-8")
    assert token not in raw
    assert device.token_sha256 in raw
    assert len(device.token_sha256) == 64
    assert json.loads(raw)["devices"][0]["token_sha256"] == device.token_sha256


def test_new_instance_reads_the_same_file(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path)
    device, token = store.issue("iPhone", "192.168.1.21", "UA")

    reloaded = DeviceStore(path)
    found = reloaded.verify(device.device_id, token)
    assert found is not None
    assert found.device_id == device.device_id
    assert found.name == "iPhone"
    assert reloaded.get(device.device_id) is not None


def test_verify_rejects_bad_credentials_and_junk(tmp_path):
    store = DeviceStore(tmp_path / "devices.json")
    device, token = store.issue("iPad", "192.168.1.20", "UA")

    assert store.verify(device.device_id, "wrong-token") is None
    assert store.verify("d_doesnotexist", token) is None
    assert store.verify(None, token) is None
    assert store.verify(device.device_id, None) is None
    assert store.verify(device.device_id, b"bytes") is None
    assert store.verify(123, token) is None
    assert store.verify(device.device_id, token * 10) is None          # > 128 chars
    assert store.verify(device.device_id, "é" * 8) is None             # non-ASCII
    assert store.verify(device.device_id + "x" * 70, token) is None    # > 64 chars


def test_revoke_invalidates_the_token(tmp_path):
    path = tmp_path / "devices.json"
    store = DeviceStore(path)
    device, token = store.issue("iPad", "192.168.1.20", "UA")

    assert store.revoke(device.device_id) is True
    assert store.verify(device.device_id, token) is None
    assert store.revoke(device.device_id) is False
    assert DeviceStore(path).verify(device.device_id, token) is None


def test_corrupt_json_is_backed_up_instead_of_crashing(tmp_path):
    path = tmp_path / "devices.json"
    path.write_text('{"devices": [oops', encoding="utf-8")

    store = DeviceStore(path)

    assert store.list() == []
    assert not path.exists()
    backups = list(tmp_path.glob("devices.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"devices": [oops'


def test_max_devices_evicts_least_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(DeviceStore, "MAX_DEVICES", 2)
    store = DeviceStore(tmp_path / "devices.json")

    d1, _ = store.issue("one", "10.0.0.1", "")
    d2, _ = store.issue("two", "10.0.0.2", "")
    store.get(d1.device_id).last_seen_at = 200.0
    store.get(d2.device_id).last_seen_at = 100.0

    d3, _ = store.issue("three", "10.0.0.3", "")
    assert store.get(d2.device_id) is None                 # least recently seen
    assert store.get(d1.device_id) is not None
    assert store.get(d3.device_id) is not None

    # touch() makes d1 the newest, so the next insert must evict d3.
    store.get(d3.device_id).last_seen_at = 300.0
    store.touch(d1.device_id, "10.0.0.9")
    d4, _ = store.issue("four", "10.0.0.4", "")
    assert store.get(d3.device_id) is None
    assert store.get(d1.device_id) is not None
    assert store.get(d4.device_id) is not None
