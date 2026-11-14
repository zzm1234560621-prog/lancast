"""Windows network report tests (subprocesses fully mocked)."""

from __future__ import annotations

import json
import os

import pytest

import lancast.net.windows as windows_module
from lancast.net import LanAddress, windows_network_report

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="windows_network_report is Windows-only"
)


def _fake_primary(monkeypatch, interface: str = "Wi-Fi") -> None:
    monkeypatch.setattr(
        windows_module,
        "lan_addresses",
        lambda: [LanAddress("192.168.1.20", interface, "lan", True)],
    )


def test_report_is_list_of_strings() -> None:
    report = windows_network_report(8443)
    assert isinstance(report, list)
    assert all(isinstance(line, str) for line in report)


@pytest.mark.parametrize("category", [0, "Public"])
def test_public_network_warning_and_firewall_hint(monkeypatch, category) -> None:
    _fake_primary(monkeypatch)

    def fake_run(script: str, timeout: float) -> str:
        if "Get-NetConnectionProfile" in script:
            return json.dumps(
                {"InterfaceAlias": "Wi-Fi", "NetworkCategory": category}
            )
        if "Get-NetFirewallPortFilter" in script:
            return "[]"
        raise AssertionError(f"unexpected script: {script}")

    monkeypatch.setattr(windows_module, "_run_powershell", fake_run)
    report = windows_network_report(8443)
    joined = "\n".join(report)

    assert "公用网络" in joined
    assert "Wi-Fi" in joined
    assert "New-NetFirewallRule" in joined
    assert "8443" in joined
    assert "-RemoteAddress LocalSubnet" in joined


def test_private_network_and_existing_rule_are_silent(monkeypatch) -> None:
    _fake_primary(monkeypatch)

    def fake_run(script: str, timeout: float) -> str:
        if "Get-NetConnectionProfile" in script:
            return '[{"InterfaceAlias":"Wi-Fi","NetworkCategory":"Private"}]'
        if "Get-NetFirewallPortFilter" in script:
            return json.dumps(
                [
                    {
                        "DisplayName": "LanCast",
                        "Enabled": "True",
                        "Action": "Allow",
                        "Direction": "Inbound",
                    }
                ]
            )
        raise AssertionError(f"unexpected script: {script}")

    monkeypatch.setattr(windows_module, "_run_powershell", fake_run)
    assert windows_network_report(8443) == []


def test_failed_queries_are_skipped_silently(monkeypatch) -> None:
    _fake_primary(monkeypatch)
    monkeypatch.setattr(
        windows_module, "_run_powershell", lambda script, timeout: None
    )
    assert windows_network_report(8443) == []


def test_numeric_public_enum_and_private_enum() -> None:
    assert windows_module._is_public(0) is True
    assert windows_module._is_public("Public") is True
    assert windows_module._is_public(1) is False
    assert windows_module._is_public("Private") is False
    assert windows_module._is_public(True) is False
