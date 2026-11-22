"""OpenUSD's first use is made by one thread alone (kernel/usd_boot).

The failure this guards against cannot be asserted directly: two threads creating a process's first stage at the same
moment stopped for good in about one run in twenty (both inside ``Usd.Stage.CreateNew``), which is a hang, not an
assertion, and needs a fresh process each time. What is asserted is the mechanism: while the first use is not done, no
second thread is inside stage creation; afterwards creation is concurrent again.
"""

from __future__ import annotations

import threading
import time

from pxr import Usd

from antigravity3d.kernel import usd_boot
from antigravity3d.kernel.usd_stage import StageManager


def _counted_create(monkeypatch):
    real = Usd.Stage.CreateNew
    seen = {"inside": 0, "peak_before_ready": 0, "peak_after_ready": 0, "calls": 0}
    guard = threading.Lock()

    def create_new(path):
        with guard:
            seen["inside"] += 1
            seen["calls"] += 1
            key = "peak_after_ready" if usd_boot.is_ready() else "peak_before_ready"
            seen[key] = max(seen[key], seen["inside"])
        try:
            time.sleep(0.05)  # hold the door open: a second thread has every chance to walk in
            return real(path)
        finally:
            with guard:
                seen["inside"] -= 1

    monkeypatch.setattr(Usd.Stage, "CreateNew", staticmethod(create_new))
    return seen


def _create_from_threads(tmp_path, n=8):
    start, errors, managers = threading.Barrier(n), [], []

    def work(k):
        try:
            start.wait(timeout=30)
            managers.append(StageManager.create(tmp_path / f"s{k}.usda", f"Asset{k}"))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and not any(t.is_alive() for t in threads), errors
    return managers


def test_the_first_stage_is_made_by_one_thread_alone_and_later_ones_together(tmp_path, monkeypatch):
    monkeypatch.setattr(usd_boot, "_ready", False)  # as in a process that has not touched OpenUSD yet
    seen = _counted_create(monkeypatch)
    managers = _create_from_threads(tmp_path)
    assert len(managers) == 8 and usd_boot.is_ready()
    assert seen["calls"] == 9, "eight stages and the one throwaway stage of the first use"
    assert seen["peak_before_ready"] == 1, "a second thread was inside stage creation before the first use was done"
    assert seen["peak_after_ready"] > 1, "after the first use, stages are still created concurrently"


def test_without_the_guard_the_same_threads_do_enter_together(tmp_path, monkeypatch):
    # the control: with ensure_ready switched off, the measurement above does see several threads inside at once
    monkeypatch.setattr(usd_boot, "_ready", False)
    monkeypatch.setattr(usd_boot, "ensure_ready", lambda: None)
    seen = _counted_create(monkeypatch)
    _create_from_threads(tmp_path)
    assert seen["peak_before_ready"] > 1


def test_ready_is_a_flag_test_after_the_first_use(monkeypatch):
    usd_boot.ensure_ready()
    called = []
    monkeypatch.setattr(Usd.Stage, "CreateNew", staticmethod(lambda path: called.append(path)))
    usd_boot.ensure_ready()
    assert usd_boot.is_ready() and not called
