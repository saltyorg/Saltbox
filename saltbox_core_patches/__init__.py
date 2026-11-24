"""Saltbox's ansible-core patches, installed in the controller process by a Saltbox vars plugin.

A vars plugin of the repository imports this package once per process and calls install_all().
ansible-core imports every vars plugin file on its vars plugin paths at startup, enabled or not,
so this happens in Saltbox, Sandbox and saltbox_mod runs before any task runs, and the forked
task workers inherit what was installed. The package lives outside vars_plugins/ because
ansible-core loads every Python file up to two directory levels below a vars plugin path as a
vars plugin.

Every module of the package that does not start with an underscore is one patch: importing it
checks its gate (_gate.py: switches, verified releases, source fingerprints) and installs it.
With -vv every patch prints whether it was installed. A patch that cannot be installed on the
running ansible-core prints one line at normal verbosity, and ansible-core's own code runs.

install_all() also decides once whether the resolver session of the saltbox_var lookup may run on
the installed ansible-core, and says so (_session_gate.py), so the task workers inherit the
verdict.
"""

from __future__ import annotations

import importlib
import pkgutil

from ._gate import PREFIX, display

INSTALLED: dict[str, bool] = {}


def install_all() -> None:
    """Import every patch module of this package once; a failing patch never stops the run."""
    for module in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
        if module.name.startswith("_") or module.name in INSTALLED:
            continue
        try:
            patch = importlib.import_module(f"{__name__}.{module.name}")
            INSTALLED[module.name] = bool(getattr(patch, "INSTALLED", False))
        except Exception as ex:  # noqa: BLE001 - any failure must leave ansible-core's own code running
            INSTALLED[module.name] = False
            display.warning(
                f"{PREFIX} {module.name}: not installed, failed to load: {ex}"
            )
    try:
        from . import _session_gate

        _session_gate.announce()
    except Exception as ex:  # noqa: BLE001 - the lookups then decide on their own
        display.warning(f"{PREFIX} resolver session gate failed: {ex}")
