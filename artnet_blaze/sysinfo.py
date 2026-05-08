"""System information for the HTTP status page.

Collected once at startup (mostly static facts) plus a few helpers that
are cheap to call on every status poll (uptimes, IP list refresh).
"""

from __future__ import annotations

import logging
import platform
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StaticInfo:
    """Fact set captured at startup. Cheap to serialize each request."""
    code_version: str = ""
    python_version: str = ""
    pyserial_version: str = ""
    pyyaml_version: str = ""
    os_pretty: str = ""
    hostname: str = ""
    process_started_at_unix: float = 0.0
    process_started_at_monotonic: float = 0.0
    poe_firmware: str = "n/a (one-way protocol)"
    dmx_dongle: str = "not configured"
    dmx_firmware: str = ""
    dmx_protocol: str = ""
    unit_name: str = ""

    @classmethod
    def collect(cls, code_version: str) -> "StaticInfo":
        return cls(
            code_version=code_version,
            python_version=platform.python_version(),
            pyserial_version=_pkg_version("serial"),
            pyyaml_version=_pkg_version("yaml"),
            os_pretty=_os_pretty(),
            hostname=socket.gethostname(),
            process_started_at_unix=time.time(),
            process_started_at_monotonic=time.monotonic(),
        )


def _pkg_version(import_name: str) -> str:
    try:
        mod = __import__(import_name)
        return getattr(mod, "__version__", "unknown")
    except ImportError:
        return "missing"


def _os_pretty() -> str:
    try:
        with open("/etc/os-release") as f:
            kv = {}
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    kv[k] = v.strip('"')
        name = kv.get("PRETTY_NAME") or kv.get("NAME")
        if name:
            return name
    except OSError:
        pass
    return platform.platform()


def system_uptime_s() -> Optional[float]:
    """Seconds since system boot, or None if not available (non-Linux)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        return None


def process_uptime_s(static: StaticInfo) -> float:
    return time.monotonic() - static.process_started_at_monotonic


def ip_addresses(log: Optional[logging.Logger] = None) -> list[str]:
    """Best-effort IPv4 list, no third-party deps.

    Aggregates from `hostname -I` (Linux) and a connect-trick fallback
    so we still return *something* in dev or on macOS.
    """
    ips: set[str] = set()

    # Linux: `hostname -I` returns space-separated v4+v6 addresses.
    try:
        out = subprocess.check_output(
            ["hostname", "-I"], timeout=1.0, stderr=subprocess.DEVNULL
        ).decode()
        for tok in out.split():
            if ":" in tok:  # skip IPv6 for now, page doesn't render them well
                continue
            if not tok.startswith("127."):
                ips.add(tok)
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        if log:
            log.debug("hostname -I unavailable: %s", e)

    # Connect-trick fallback (works on macOS / dev machines).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            ips.add(ip)
    except OSError:
        pass

    return sorted(ips)


def has_network_ip(timeout_s: float = 0.1) -> bool:
    """Fast yes/no: does this host have any non-loopback IPv4?

    Uses the connect-trick — open a UDP socket to a non-routable address,
    read back the local sockname. No packets sent. Returns False if the
    socket layer can't pick a non-loopback source (network down, no IP).
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout_s)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return bool(ip and not ip.startswith("127."))
    except OSError:
        return False


def fmt_duration(seconds: Optional[float]) -> str:
    """Human-readable duration: "3d 14h 22m" / "1h 5m" / "42s"."""
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@dataclass
class LiveInfo:
    """Things that change while the daemon runs."""
    process_uptime_s: float = 0.0
    process_uptime_human: str = ""
    system_uptime_s: Optional[float] = None
    system_uptime_human: str = ""
    ip_addresses: list[str] = field(default_factory=list)

    @classmethod
    def snapshot(cls, static: StaticInfo, log: Optional[logging.Logger] = None) -> "LiveInfo":
        proc_up = process_uptime_s(static)
        sys_up = system_uptime_s()
        return cls(
            process_uptime_s=proc_up,
            process_uptime_human=fmt_duration(proc_up),
            system_uptime_s=sys_up,
            system_uptime_human=fmt_duration(sys_up),
            ip_addresses=ip_addresses(log),
        )
