"""Build no variables for ansible-core's own bookkeeping meta tasks.

ansible-core adds meta tasks to every play by itself: role_complete after each role,
flush_handlers after each play section and noop placeholders in force_handlers plays. The
built-in strategies build the host's complete variable set for each of them, as for any task,
although they only template a few keywords of these tasks with it. In a play with many roles
(Saltbox lists about a hundred, Sandbox about two hundred) that is most of the variable building
of a run.

This patch wraps VariableManager.get_vars. When the run() method of ansible-core's linear or
free strategy asks for the variables of such a task, and every keyword that strategy templates
for it is a plain value, the call returns an empty mapping:
  - linear: any_errors_fatal (the action of these tasks is always the plain "meta");
  - free: throttle, run_once and name.
The same run() methods also serve every strategy that inherits them without overriding them:
debug and host_pinned, and third-party subclasses of linear or free, whose other overridden
methods then see the empty mapping too. Every other call, including any from a strategy with its
own run(), from plugins or for when conditions, gets the real variables, and so does any call for
which the decision itself fails.

Switch: SALTBOX_SKIP_BOOKKEEPING_VARS, or skip_bookkeeping_vars in the [saltbox_core_patches]
section of ansible.cfg (see _gate.py). With -vv the run ends with the number of calls answered.
"""

from __future__ import annotations

import atexit
import os
import sys
from collections.abc import Callable
from types import CodeType, FrameType
from typing import Any

# ansible-core ships no py.typed marker, so its imports are untyped.
from ansible import constants  # type: ignore[import-untyped]
from ansible.vars.manager import VariableManager  # type: ignore[import-untyped]

from ._gate import PREFIX, Target, allowed, display

PATCH = "skip_bookkeeping_vars"
SWITCH = "SALTBOX_SKIP_BOOKKEEPING_VARS"
# The ansible-core functions this patch relies on: the strategy loops it answers (which template
# only the keywords checked below for these tasks), the meta task execution that follows (which
# reads no variables for role_complete and builds its own for when conditions), the variable
# hook the loops call after get_vars, the wrapped get_vars signature, the meta test, and the
# two places that create the implicit tasks.
TARGETS: tuple[Target, ...] = (
    ("plugins/strategy/linear.py", "StrategyModule.run"),
    ("plugins/strategy/free.py", "StrategyModule.run"),
    ("plugins/strategy/__init__.py", "StrategyBase._execute_meta"),
    ("plugins/strategy/__init__.py", "StrategyBase.add_tqm_variables"),
    ("vars/manager.py", "VariableManager.get_vars"),
    ("playbook/task.py", "Task._get_meta"),
    ("playbook/role/__init__.py", "Role.compile"),
    ("playbook/play.py", "Play.compile"),
    # host_pinned and debug reuse free's and linear's run(); their classes must not override it.
    ("plugins/strategy/host_pinned.py", "StrategyModule"),
    ("plugins/strategy/debug.py", "StrategyModule"),
)
# Releases this patch was run against.
VERIFIED_RELEASES = frozenset(
    {"2.21.2", "2.21.3", "2.21.4", "2.21.5", "2.21.6", "2.22.0"}
)
# 2.21.2 to 2.21.6; 2.22.0 (free and linear changed in how they track blocked hosts).
VERIFIED_FINGERPRINTS = frozenset(
    {
        "0d0f44910a2587e3cc8fcc3101df960d8c362aeaada00f6d7f81f1764dbc9cfe",
        "99425c36a00b34e236149a6b47d9a2b4028963cf55de9353181790e09d1c34bb",
    }
)
BOOKKEEPING_META = frozenset({"role_complete", "flush_handlers", "noop"})
STATS: dict[str, int] = {"skipped": 0, "passed": 0, "failed": 0}
MARKER = "_saltbox_skip_bookkeeping_vars"
INSTALLED = False

Check = Callable[[Any], bool]
_CALLERS: dict[tuple[str, str, int], tuple[Check, ...]] = {}


def _plain_bool(value: Any) -> bool:
    return value is None or isinstance(value, bool)


def _linear_plain(task: Any) -> bool:
    return _plain_bool(task.any_errors_fatal)


def _free_plain(task: Any) -> bool:
    return (
        isinstance(task.throttle, int) and _plain_bool(task.run_once) and not task.name
    )


def _code_key(code: CodeType) -> tuple[str, str, int]:
    return (os.path.realpath(code.co_filename), code.co_name, code.co_firstlineno)


def _callers() -> dict[tuple[str, str, int], tuple[Check, ...]]:
    """Map the code of the strategy loops answered with no variables to their checks."""
    if not _CALLERS:
        from ansible.plugins.strategy.free import (  # type: ignore[import-untyped]
            StrategyModule as FreeStrategy,
        )
        from ansible.plugins.strategy.linear import (  # type: ignore[import-untyped]
            StrategyModule as LinearStrategy,
        )

        _CALLERS[_code_key(LinearStrategy.run.__code__)] = (_linear_plain,)
        _CALLERS[_code_key(FreeStrategy.run.__code__)] = (_free_plain,)
    return _CALLERS


def needs_no_variables(task: Any, frame: FrameType | None) -> bool:
    """Return whether this get_vars call, made from frame, may be answered with no variables."""
    if getattr(task, "implicit", False) is not True:
        return False
    if (
        task.action not in constants._ACTION_META
        or task._get_meta() not in BOOKKEEPING_META
    ):
        return False
    # Step over other wrappers of get_vars that keep its name (profilers, for example) to the
    # real caller; behind a wrapper with another name, no call is answered.
    while frame is not None and frame.f_code.co_name == "get_vars":
        frame = frame.f_back
    if frame is None:
        return False
    checks = _callers().get(_code_key(frame.f_code))
    return checks is not None and all(check(task) for check in checks)


def _report(pid: int) -> None:
    """At -vv, print the counters at the end of the run (stderr, as stdout may be closed)."""
    if os.getpid() != pid or display.verbosity < 2:
        return
    if not (STATS["skipped"] or STATS["passed"]):
        return
    try:
        display.display(
            f"{PREFIX} {PATCH}: answered {STATS['skipped']} of "
            f"{STATS['skipped'] + STATS['passed']} get_vars calls with no variables"
            + (f"; {STATS['failed']} decisions failed" if STATS["failed"] else ""),
            stderr=True,
        )
    except (OSError, ValueError):
        pass


def install() -> bool:
    """Wrap VariableManager.get_vars if the gate allows it."""
    if getattr(VariableManager.get_vars, MARKER, False):
        return True
    reason = allowed(PATCH, SWITCH, VERIFIED_RELEASES, VERIFIED_FINGERPRINTS, TARGETS)
    if reason is None:
        return False
    original = VariableManager.get_vars

    def get_vars(
        self: Any,
        play: Any = None,
        host: Any = None,
        task: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if task is not None:
            try:
                skip = needs_no_variables(task, sys._getframe(1))
            except Exception as ex:  # noqa: BLE001 - a failed decision keeps ansible-core's own code
                skip = False
                if not STATS["failed"]:
                    display.vv(
                        f"{PREFIX} {PATCH}: decision failed, real variables used: {ex}"
                    )
                STATS["failed"] += 1
            if skip:
                STATS["skipped"] += 1
                return {}
        STATS["passed"] += 1
        return original(self, play, host, task, *args, **kwargs)

    get_vars.__doc__ = original.__doc__
    get_vars.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(get_vars, MARKER, True)
    VariableManager.get_vars = get_vars
    atexit.register(_report, os.getpid())
    display.vv(f"{PREFIX} {PATCH}: installed ({reason})")
    return True


INSTALLED = install()
