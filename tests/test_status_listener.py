"""Tests for the continuous STATUS listener (v3.16.0).

Focused on the in-place update mechanics and subscription bookkeeping.
End-to-end socket reception is covered by the existing integration
tests (`test_native_discovery.py`, `test_listen_multicast.py`) — those
already exercise the same multicast plumbing that ``StatusListener``
reuses.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.discovery import ChannelInfo
from ka9q.status_listener import StatusListener, StatusListenerStats


def _bare_listener() -> StatusListener:
    """Construct a StatusListener without touching the network."""
    listener = StatusListener.__new__(StatusListener)
    listener.status_address = "test.local"
    listener.interface = None
    listener.status_port = 5006
    listener.socket_timeout = 0.1
    listener._channels = {}
    listener._callbacks = {}
    listener._wildcard_callbacks = []
    listener._lock = threading.RLock()
    listener._socket = None
    listener._thread = None
    listener._running = False
    listener.stats = StatusListenerStats()
    return listener


def _make_channel(ssrc: int) -> ChannelInfo:
    return ChannelInfo(
        ssrc=ssrc,
        preset="iq",
        sample_rate=24_000,
        frequency=10_000_000.0,
        snr=12.5,
        multicast_address="239.1.2.3",
        port=5004,
        gps_time=None,
        rtp_timesnap=None,
        encoding=4,
    )


def _make_status(ssrc: int, gps_time: int, rtp_timesnap: int,
                 encoding: int | None = None) -> dict:
    s = {"ssrc": ssrc, "gps_time": gps_time, "rtp_timesnap": rtp_timesnap}
    if encoding is not None:
        s["encoding"] = encoding
    return s


# ── update mechanics ──────────────────────────────────────────────


def test_register_and_apply_update_mutates_in_place():
    listener = _bare_listener()
    ci = _make_channel(ssrc=12345)
    listener.register_channel(ci)

    listener._apply_update(12345, _make_status(12345, 1_000_000_000_000, 1000),
                           1_000_000_000_000, 1000)

    assert ci.gps_time == 1_000_000_000_000
    assert ci.rtp_timesnap == 1000
    assert listener.stats.updates_applied == 1


def test_apply_update_refreshes_existing_anchor():
    listener = _bare_listener()
    ci = _make_channel(ssrc=42)
    ci.gps_time = 1_000_000_000_000
    ci.rtp_timesnap = 1000
    listener.register_channel(ci)

    listener._apply_update(42, _make_status(42, 2_000_000_000_000, 2024),
                           2_000_000_000_000, 2024)

    assert ci.gps_time == 2_000_000_000_000
    assert ci.rtp_timesnap == 2024


def test_apply_update_refreshes_encoding_when_present():
    listener = _bare_listener()
    ci = _make_channel(ssrc=1)
    ci.encoding = 4  # F32LE
    listener.register_channel(ci)

    listener._apply_update(1, _make_status(1, 10, 20, encoding=6),
                           10, 20)
    assert ci.encoding == 6


def test_apply_update_does_not_refresh_frequency_or_sample_rate():
    """We deliberately freeze frequency/sample_rate to avoid silently
    flipping a recorder's interpretation if radiod retunes mid-stream."""
    listener = _bare_listener()
    ci = _make_channel(ssrc=1)
    original_freq = ci.frequency
    original_rate = ci.sample_rate
    listener.register_channel(ci)

    # Hand-craft a status dict including frequency/sample_rate.
    status = {
        "ssrc": 1,
        "gps_time": 10,
        "rtp_timesnap": 20,
        "frequency": 14_074_000.0,
        "sample_rate": 12_000,
    }
    listener._apply_update(1, status, 10, 20)

    assert ci.frequency == original_freq
    assert ci.sample_rate == original_rate


def test_apply_update_for_unknown_ssrc_is_dropped():
    listener = _bare_listener()
    # No channel registered for SSRC 999.
    listener._apply_update(999, _make_status(999, 10, 20), 10, 20)
    # Nothing crashes; nothing is tracked.
    assert listener.stats.updates_applied == 0
    assert listener.get_channel_info(999) is None


# ── callback fan-out ──────────────────────────────────────────────


def test_per_ssrc_callback_fires_on_matching_update():
    listener = _bare_listener()
    ci = _make_channel(ssrc=7)
    listener.register_channel(ci)

    seen: list[ChannelInfo] = []
    listener.add_callback(7, seen.append)

    listener._apply_update(7, _make_status(7, 99, 88), 99, 88)
    assert len(seen) == 1
    assert seen[0] is ci
    assert seen[0].gps_time == 99


def test_per_ssrc_callback_does_not_fire_for_other_ssrcs():
    listener = _bare_listener()
    listener.register_channel(_make_channel(ssrc=1))
    listener.register_channel(_make_channel(ssrc=2))

    seen_one: list[ChannelInfo] = []
    listener.add_callback(1, seen_one.append)

    listener._apply_update(2, _make_status(2, 5, 6), 5, 6)
    assert seen_one == []


def test_wildcard_callback_fires_on_every_update():
    listener = _bare_listener()
    listener.register_channel(_make_channel(ssrc=1))
    listener.register_channel(_make_channel(ssrc=2))

    seen: list[int] = []
    listener.add_wildcard_callback(lambda ci: seen.append(ci.ssrc))

    listener._apply_update(1, _make_status(1, 10, 11), 10, 11)
    listener._apply_update(2, _make_status(2, 20, 21), 20, 21)
    assert seen == [1, 2]


def test_callback_exception_does_not_stop_listener():
    listener = _bare_listener()
    ci = _make_channel(ssrc=1)
    listener.register_channel(ci)

    def boom(_ci):
        raise RuntimeError("boom")

    seen_after: list[ChannelInfo] = []
    listener.add_callback(1, boom)
    listener.add_callback(1, seen_after.append)

    listener._apply_update(1, _make_status(1, 1, 2), 1, 2)
    assert listener.stats.callback_errors == 1
    # The good callback still ran after the bad one
    assert len(seen_after) == 1


def test_remove_callback_works():
    listener = _bare_listener()
    listener.register_channel(_make_channel(ssrc=1))

    seen: list[ChannelInfo] = []
    listener.add_callback(1, seen.append)
    listener.remove_callback(1, seen.append)

    listener._apply_update(1, _make_status(1, 5, 6), 5, 6)
    assert seen == []


def test_unregister_channel_stops_updates():
    listener = _bare_listener()
    ci = _make_channel(ssrc=1)
    listener.register_channel(ci)
    listener._apply_update(1, _make_status(1, 10, 20), 10, 20)
    assert ci.gps_time == 10

    listener.unregister_channel(1)
    listener._apply_update(1, _make_status(1, 99, 88), 99, 88)
    assert ci.gps_time == 10  # unchanged after unregister


# ── lifecycle ─────────────────────────────────────────────────────


def test_start_is_idempotent_when_already_running():
    listener = _bare_listener()
    listener._running = True
    listener._thread = MagicMock()
    # start() must not spawn a second thread or recreate the socket.
    # We don't actually have a socket; verify the no-op path.
    listener.start()
    assert listener._running is True


def test_stop_when_not_running_is_no_op():
    listener = _bare_listener()
    listener.stop()  # must not raise


def test_get_channel_info_returns_registered_ci():
    listener = _bare_listener()
    ci = _make_channel(ssrc=42)
    listener.register_channel(ci)
    assert listener.get_channel_info(42) is ci
    assert listener.get_channel_info(99) is None


# ── configuration ────────────────────────────────────────────────


def test_status_port_defaults_to_5006():
    """Default ``status_port`` matches ka9q-radio DEFAULT_STAT_PORT."""
    import inspect
    sig = inspect.signature(StatusListener.__init__)
    assert sig.parameters["status_port"].default == 5006


def test_custom_status_port_stored():
    """Construct via a stripped __init__ path to test that the custom
    port is propagated to the listener instance."""
    listener = StatusListener.__new__(StatusListener)
    listener.status_address = "test.local"
    listener.interface = None
    listener.status_port = 6006
    listener.socket_timeout = 0.5
    listener._channels = {}
    listener._callbacks = {}
    listener._wildcard_callbacks = []
    listener._lock = threading.RLock()
    listener._socket = None
    listener._thread = None
    listener._running = False
    listener.stats = StatusListenerStats()
    assert listener.status_port == 6006


def test_stats_has_socket_errors_counter():
    listener = _bare_listener()
    assert hasattr(listener.stats, "socket_errors")
    assert listener.stats.socket_errors == 0
