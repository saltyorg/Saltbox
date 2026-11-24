"""Keep the garbage collector of each task worker away from the objects it inherits.

ansible-core forks a worker process for every task. The worker inherits the controller's whole
heap: the loaded playbooks, roles, inventory and variables, millions of objects. Python's cyclic
garbage collector then keeps walking all of them in the worker, triggered by the objects the
task's templating allocates, although the worker cannot make any of them unreachable that the
controller still holds. On Python 3.14 that walk is a large share of each worker's time, and it
also writes to every page it visits, which undoes the sharing of memory with the controller.

This patch calls gc.freeze() at the start of WorkerProcess.run, in the worker only, as the
Python documentation recommends for forked children: the inherited objects move to the permanent
generation, which the collector skips. Objects the worker creates are collected as before. An
inherited object that becomes unreachable in the worker is freed by reference counting as before;
only one in a reference cycle stays in memory until the worker exits. No variable, value or
result changes, and the controller is not affected.

Switch: SALTBOX_FREEZE_WORKER_GC, or freeze_worker_gc in the [saltbox_core_patches] section of
ansible.cfg (see _gate.py).
"""

from __future__ import annotations

import functools
import gc
from typing import Any

# ansible-core ships no py.typed marker, so its imports are untyped.
from ansible.executor.process.worker import (  # type: ignore[import-untyped]
    WorkerProcess,
)

from ._gate import PREFIX, Target, allowed, display

PATCH = "freeze_worker_gc"
SWITCH = "SALTBOX_FREEZE_WORKER_GC"
# The ansible-core code this patch relies on: WorkerProcess.run is the entry point the forked
# worker runs, started through ansible-core's fork context.
TARGETS: tuple[Target, ...] = (
    ("executor/process/worker.py", "WorkerProcess.run"),
    ("executor/process/worker.py", "WorkerProcess.start"),
    ("utils/multiprocessing.py", "refs:context"),
)
# Releases this patch was run against.
VERIFIED_RELEASES = frozenset({"2.21.2", "2.22.0"})
# 2.21.2, then 2.22.0.
VERIFIED_FINGERPRINTS = frozenset(
    {
        "023d591155a930677340dc7d4e7d4934869d9c00f4d78881b8a0e2c500125150",
        "75aee9ac298eab6c32c98b2ecf5bd785387b09dad3d87bd70ab1e078096e97ba",
    }
)
MARKER = "_saltbox_freeze_worker_gc"
INSTALLED = False


def install() -> bool:
    """Wrap WorkerProcess.run if the gate allows it."""
    if getattr(WorkerProcess.run, MARKER, False):
        return True
    reason = allowed(PATCH, SWITCH, VERIFIED_RELEASES, VERIFIED_FINGERPRINTS, TARGETS)
    if reason is None:
        return False
    if getattr(WorkerProcess, "_start_method", None) != "fork":
        display.display(
            f"{PREFIX} {PATCH}: not installed, task workers are not started with fork",
            stderr=True,
        )
        return False
    original = WorkerProcess.run

    @functools.wraps(original)
    def run(self: Any) -> Any:
        gc.freeze()
        return original(self)

    setattr(run, MARKER, True)
    WorkerProcess.run = run
    display.vv(f"{PREFIX} {PATCH}: installed ({reason})")
    return True


INSTALLED = install()
