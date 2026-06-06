import gc
import unittest
import weakref

import numpy as np

from ka9q.multi_stream import MultiStream, _ChannelSlot
from ka9q.stream_quality import StreamQuality


def _slot(freq_hz, ring):
    """A slot whose on_samples closure captures ``ring`` — mirroring the real
    sink chain (sink.on_samples -> BandRecorder -> RingBuffer._samples)."""

    def on_samples(samples, *a, **k):
        return len(ring)

    return _ChannelSlot(
        channel_info=None,
        frequency_hz=freq_hz,
        preset="usb",
        sample_rate=12000,
        encoding=2,
        is_iq=False,
        resequencer=None,
        quality=StreamQuality(),
        on_samples=on_samples,
        on_stream_dropped=None,
        on_stream_restored=None,
    )


class TestMultiStreamPruneFrequency(unittest.TestCase):
    """prune_frequency must release the superseded slot's ring on reprovision.

    Regression guard for the 2026-06-06 leak: re-provisioning a stale band
    landed a fresh SSRC + sink while the old slot lingered in _slots, its
    on_samples closure pinning the old decoder ring (~86 MB for a 30-min
    FST4W ring) forever — ~GB/hour under a flaky radiod.
    """

    def setUp(self):
        # MultiStream ctor only stores control; no socket/I-O is opened.
        self.m = MultiStream(control=None)
        self.FREQ = 14_097_100.0

    def test_old_ring_is_released(self):
        old_ring = np.zeros(21_600_000, dtype=np.float32)  # ~86 MB
        new_ring = np.zeros(1_440_000, dtype=np.float32)
        old_w = weakref.ref(old_ring)
        new_w = weakref.ref(new_ring)

        self.m._slots[111] = _slot(self.FREQ, old_ring)          # stale, old SSRC
        self.m._slots[222] = _slot(self.FREQ, new_ring)          # reprovisioned
        self.m._slots[333] = _slot(7_038_600.0, np.zeros(10))    # other band
        del old_ring, new_ring  # only the slots' closures hold the rings now

        removed = self.m.prune_frequency(self.FREQ, keep_ssrc=222)
        gc.collect()

        self.assertEqual(removed, [111])
        self.assertIn(222, self.m._slots)
        self.assertNotIn(111, self.m._slots)
        self.assertIn(333, self.m._slots, "unrelated band must survive")
        self.assertIsNone(old_w(), "superseded ring must be garbage-collected")
        self.assertIsNotNone(new_w(), "kept ring must stay alive")

    def test_noop_when_nothing_to_prune(self):
        only = np.zeros(10, dtype=np.float32)
        self.m._slots[222] = _slot(self.FREQ, only)
        self.assertEqual(self.m.prune_frequency(self.FREQ, keep_ssrc=222), [])
        self.assertEqual(self.m.prune_frequency(9_999_000.0, keep_ssrc=222), [])
        self.assertIn(222, self.m._slots)

    def test_prunes_all_other_ssrcs_on_frequency(self):
        for ss in (10, 11, 12, 13):
            self.m._slots[ss] = _slot(self.FREQ, np.zeros(4))
        removed = self.m.prune_frequency(self.FREQ, keep_ssrc=13)
        self.assertEqual(sorted(removed), [10, 11, 12])
        self.assertEqual(list(self.m._slots), [13])


if __name__ == "__main__":
    unittest.main()
