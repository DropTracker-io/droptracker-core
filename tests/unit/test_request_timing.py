"""Slow-request accounting for deliberately held requests (api/request_timing.py).

GET /notifications?wait=25 parks a request on purpose; the after_request
slow-request log must judge the work time (total minus hold), not the wall
time, or every expired hold logs as a 25,000ms request. Loaded from the file
path so the conftest's package stubs never get in the way.
"""
import importlib.util
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


rt = _load("_request_timing_under_test", "api", "request_timing.py")

THRESHOLD_MS = 750.0


class TestHoldAccounting:
    def test_no_hold_recorded_means_zero(self):
        g = SimpleNamespace()
        assert rt.held_ms(g) == 0.0
        assert rt.work_ms(812.4, rt.held_ms(g)) == 812.4

    def test_record_hold_accumulates_in_ms(self):
        g = SimpleNamespace()
        rt.record_hold(g, 24.998)
        rt.record_hold(g, 0.002)
        assert abs(rt.held_ms(g) - 25000.0) < 1e-6

    def test_garbage_and_negative_holds_are_ignored(self):
        g = SimpleNamespace()
        rt.record_hold(g, None)
        rt.record_hold(g, "nope")
        rt.record_hold(g, -3)
        assert rt.held_ms(g) == 0.0

    def test_expired_long_poll_is_not_slow(self):
        # The production pattern: ~25,000ms wall time, ~5ms of actual work.
        g = SimpleNamespace()
        rt.record_hold(g, 24.99821)
        total = 25004.79
        assert rt.work_ms(total, rt.held_ms(g)) < THRESHOLD_MS

    def test_slow_work_around_a_hold_still_flags(self):
        # A hold must not hide a genuinely slow identity lookup beside it.
        g = SimpleNamespace()
        rt.record_hold(g, 10.0)
        total = 10000.0 + 900.0
        assert rt.work_ms(total, rt.held_ms(g)) >= THRESHOLD_MS

    def test_work_never_negative(self):
        # Clock skew between the two perf_counter reads cannot go below zero.
        assert rt.work_ms(100.0, 150.0) == 0.0

    def test_describe_plain_and_held(self):
        assert rt.describe(812.4, 0.0) == "812.40 ms"
        assert rt.describe(25004.79, 24998.1) == (
            "25004.79 ms (24998.10 ms held, 6.69 ms work)")
