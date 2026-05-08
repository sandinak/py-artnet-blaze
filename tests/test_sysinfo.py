"""sysinfo helpers: version collection, OS pretty-name, uptime, durations."""

from __future__ import annotations

import time

import pytest

from artnet_blaze.sysinfo import (
    LiveInfo,
    StaticInfo,
    fmt_duration,
    has_network_ip,
    ip_addresses,
    process_uptime_s,
    system_uptime_s,
)


def test_static_info_collects_basics():
    info = StaticInfo.collect("0.9.9")
    assert info.code_version == "0.9.9"
    assert info.python_version  # something like "3.11.5"
    assert info.hostname
    assert info.process_started_at_unix > 0
    # Defaults preserved for fields filled later by setup_app
    assert "one-way" in info.poe_firmware


def test_pkg_versions_resolved():
    info = StaticInfo.collect("test")
    # Package versions either resolve, or we get our explicit fallbacks.
    assert info.pyserial_version not in ("", None)
    assert info.pyyaml_version not in ("", None)


@pytest.mark.parametrize("seconds,expected", [
    (None, "n/a"),
    (5, "5s"),
    (65, "1m 5s"),
    (3700, "1h 1m"),
    (90061, "1d 1h 1m"),
])
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_process_uptime_increases_over_time():
    info = StaticInfo.collect("test")
    u1 = process_uptime_s(info)
    time.sleep(0.01)
    u2 = process_uptime_s(info)
    assert u2 > u1
    assert u2 >= 0.01


def test_system_uptime_returns_float_or_none():
    val = system_uptime_s()
    # Linux: float. macOS/CI without /proc: None. Both acceptable.
    assert val is None or (isinstance(val, float) and val > 0)


def test_ip_addresses_returns_list_with_no_loopback():
    ips = ip_addresses()
    assert isinstance(ips, list)
    for ip in ips:
        assert not ip.startswith("127."), ip


def test_has_network_ip_returns_bool():
    """Smoke test — actual answer depends on the test runner's network,
    but the function should return a bool quickly and not raise."""
    val = has_network_ip(timeout_s=0.5)
    assert isinstance(val, bool)


def test_live_info_snapshot_populates_human_strings():
    info = StaticInfo.collect("test")
    live = LiveInfo.snapshot(info)
    assert live.process_uptime_human  # at least "0s"
    # If /proc/uptime is missing, system_uptime_human should be "n/a"
    assert live.system_uptime_human in ("n/a",) or "s" in live.system_uptime_human or "m" in live.system_uptime_human or "h" in live.system_uptime_human or "d" in live.system_uptime_human
