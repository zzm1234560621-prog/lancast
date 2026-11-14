"""Read-only Windows network diagnostics (Chinese messages)."""

from __future__ import annotations

import json
import os
import subprocess

from .addresses import LanAddress, lan_addresses

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROFILE_TIMEOUT = 4.0
_FIREWALL_TIMEOUT = 10.0

_PROFILE_SCRIPT = (
    "Get-NetConnectionProfile | "
    "Select-Object InterfaceAlias,NetworkCategory | ConvertTo-Json"
)


def _run_powershell(script: str, timeout: float) -> str | None:
    """Run a read-only PowerShell snippet; ``None`` means the query failed."""

    if os.name != "nt":
        return None

    command = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None
    return completed.stdout or ""


def _load_json(text: str | None):
    if not text:
        return None
    raw = text.lstrip("\ufeff").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _primary_entry() -> LanAddress | None:
    try:
        addresses = lan_addresses()
    except Exception:
        return None

    for address in addresses:
        if address.kind == "lan":
            return address
    for address in addresses:
        if address.kind != "loopback":
            return address
    return None


def _is_public(category: object) -> bool:
    """Accept both the numeric enum (0) and the string form ("Public")."""

    if isinstance(category, bool):
        return False
    if isinstance(category, int):
        return category == 0
    text = str(category).strip().casefold()
    return text in {"0", "public", "publicnetwork", "公用", "公用网络"}


def _network_categories() -> dict[str, object] | None:
    raw = _run_powershell(_PROFILE_SCRIPT, _PROFILE_TIMEOUT)
    if raw is None:
        return None

    data = _load_json(raw)
    if data is None:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    categories: dict[str, object] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        alias = item.get("InterfaceAlias")
        if alias is None:
            continue
        categories[str(alias).casefold()] = item.get("NetworkCategory")
    return categories


def _firewall_script(port: int) -> str:
    return (
        "$filters = @(Get-NetFirewallPortFilter -Protocol TCP -ErrorAction Stop | "
        f"Where-Object {{ $_.LocalPort -eq {port} }}); "
        "if ($filters.Count -eq 0) { '[]' } else { "
        "$rules = @($filters | Get-NetFirewallRule -ErrorAction Stop | "
        "Where-Object { $_.Enabled -eq 'True' -and $_.Action -eq 'Allow' "
        "-and $_.Direction -eq 'Inbound' }); "
        "$rules | Select-Object DisplayName,Enabled,Action,Direction | ConvertTo-Json }"
    )


def _rule_allows(rule: dict) -> bool:
    enabled = rule.get("Enabled")
    if isinstance(enabled, bool):
        enabled_ok = enabled
    else:
        enabled_ok = str(enabled).strip().casefold() in {"true", "1", "yes"}

    action = str(rule.get("Action", "")).strip().casefold()
    direction = str(rule.get("Direction", "")).strip().casefold()
    return enabled_ok and action == "allow" and direction == "inbound"


def _has_allowing_rule(port: int) -> bool | None:
    """True/False for a successful query, ``None`` when the check failed."""

    raw = _run_powershell(_firewall_script(port), _FIREWALL_TIMEOUT)
    if raw is None:
        return None
    if not raw.strip():
        # A successful query with no matching rules prints nothing.
        return False

    data = _load_json(raw)
    if data is None:
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None

    for rule in data:
        if isinstance(rule, dict) and _rule_allows(rule):
            return True
    return False


def _append_public_warning(report: list[str]) -> None:
    entry = _primary_entry()
    if entry is None:
        return

    categories = _network_categories()
    if not categories:
        return

    if not _is_public(categories.get(entry.interface.casefold())):
        return

    report.append(
        f"当前网络「{entry.interface}」被 Windows 标记为“公用网络”，"
        "防火墙默认会阻止手机访问。建议：设置 → 网络和 Internet → WLAN → "
        "该网络 → 网络配置文件类型 选「专用网络」。"
    )


def _append_firewall_hint(report: list[str], port: int) -> None:
    allowed = _has_allowing_rule(port)
    if allowed is None or allowed:
        return

    report.append(
        f"未找到允许 TCP {port} 入站连接的已启用 Windows 防火墙规则。"
        "如需放行，请以管理员身份运行以下命令（仅提示，不会自动执行）："
    )
    report.append(
        'New-NetFirewallRule -DisplayName "LanCast" -Direction Inbound '
        f"-Protocol TCP -LocalPort {port} -Action Allow "
        "-Profile Private,Public -RemoteAddress LocalSubnet"
    )


def windows_network_report(port: int) -> list[str]:
    """Read-only Windows diagnostics; never raises and returns ``[]`` off Windows."""

    if os.name != "nt":
        return []

    report: list[str] = []
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return report

    try:
        _append_public_warning(report)
    except Exception:
        pass
    try:
        _append_firewall_hint(report, port_number)
    except Exception:
        pass
    return report
