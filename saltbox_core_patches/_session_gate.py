"""Gate of the resolver session of the saltbox_var lookup (lookup_plugins/saltbox_var.py).

The session builds on ansible-core's templating internals and on Jinja2's template node types, so
it runs only when the source of every one of them is identical to a release it was verified
against with the identical-output tests (a fingerprint of the unparsed syntax trees, see
_fingerprint.py, so formatting and comments do not count). The version
number itself is not used: a new release that leaves all of them unchanged keeps the session,
and one that changes any of them switches it off until it is verified.

When it is off, role_var, role_web, docker_vars and saltbox_var render every reference
separately (slower, same results) and the controller prints one line at normal verbosity saying
why; with -vv it prints the fingerprint when the session may run. The verdict is decided once per
process: saltbox_config imports this package in the controller before any task runs, so the
forked task workers inherit it. A lookup in a process where saltbox_config was not loaded decides
it itself (see saltbox_var), and says why only with -vvvv.

To verify a release: run the identical-output tests on it, then add the fingerprint the off line
(or fingerprint_now()) shows to VERIFIED_FINGERPRINTS, with the releases it was verified on.
Python 3.12.13 to 3.15.0 compute the same fingerprints; the Python 3.12.3 of Ubuntu 24.04 does
not (it unparses Jinja2's nodes.py differently), so compute them with the Python Saltbox installs.
"""

from __future__ import annotations

import configparser
import hashlib
import os

import jinja2
from ansible import constants  # type: ignore[import-untyped]
from ansible.module_utils.parsing.convert_bool import (  # type: ignore[import-untyped]
    boolean,
)

from ._fingerprint import Target, fingerprint
from ._gate import ANSIBLE_CORE_VERSION, ANSIBLE_ROOT, display

PREFIX = "[saltbox_var]"
SWITCH = "SALTBOX_LOOKUP_SESSION"
SECTION = "saltbox_lookups"
JINJA_ROOT = os.path.dirname(os.path.abspath(jinja2.__file__))

# The ansible-core code the session relies on: the whole templating engine it subclasses and
# whose lazy variable views, compile path and lookup invoker it reuses or wraps, the Templar it
# hands to the lookups, the tag types and helpers of the values it copies, the traceback switch
# of replayed warnings, and the Display methods it records and replays.
TARGETS: tuple[Target, ...] = (
    ("_internal/_templating/_engine.py", "<module>"),
    ("_internal/_templating/_jinja_bits.py", "<module>"),
    ("_internal/_templating/_jinja_common.py", "<module>"),
    ("_internal/_templating/_jinja_plugins.py", "<module>"),
    ("_internal/_templating/_lazy_containers.py", "<module>"),
    ("_internal/_templating/_utils.py", "<module>"),
    ("template/__init__.py", "<module>"),
    ("module_utils/_internal/_datatag/__init__.py", "<module>"),
    ("module_utils/_internal/_traceback.py", "<module>"),
    ("utils/display.py", "Display"),
    # The lookup invoker is wrapped by replacing the module attribute; every caller must look it
    # up there when it calls it.
    (".", "files:_invoke_lookup"),
)
# Jinja2's node types: a render is reused only when its template has no node that can make it
# depend on more than its variables (includes, imports, blocks and the like).
JINJA_TARGETS: tuple[Target, ...] = (("nodes.py", "<module>"),)
# Fingerprints the identical-output tests passed on, with the releases they were run on.
VERIFIED_FINGERPRINTS: dict[str, str] = {
    "c9493109220b2a04785e8e0c30af8435584c1e86d250660d267a8eca55af0053": (
        "verified on ansible-core 2.21.2 to 2.21.6 with Jinja2 3.1.6"
    ),
    "4cb691470a0ddc3f28199f934dcb2e62af41a15403e65bc12050288fe9b7ab72": (
        "verified on ansible-core 2.22.0 with Jinja2 3.1.6"
    ),
}

_VERDICT: dict[str, str | None] = {}
ANNOUNCED = False


def fingerprint_now() -> str:
    """Return the fingerprint of the installed ansible-core and Jinja2 the session relies on."""
    core = fingerprint(TARGETS, ANSIBLE_ROOT)
    jinja = fingerprint(JINJA_TARGETS, JINJA_ROOT)
    return hashlib.sha256(f"ansible:{core}\njinja2:{jinja}\n".encode()).hexdigest()


def unverified() -> str | None:
    """Return why the session may not run on the installed release, or None when it may."""
    if "verdict" not in _VERDICT:
        try:
            found = fingerprint_now()
        except (OSError, SyntaxError, ValueError, LookupError) as ex:
            _VERDICT["fingerprint"] = None
            _VERDICT["verdict"] = (
                f"ansible-core {ANSIBLE_CORE_VERSION} could not be checked ({ex})"
            )
        else:
            _VERDICT["fingerprint"] = found
            _VERDICT["verdict"] = (
                None
                if found in VERIFIED_FINGERPRINTS
                else (
                    f"ansible-core {ANSIBLE_CORE_VERSION} with Jinja2 {jinja2.__version__} is "
                    "not verified for it (the internals it relies on differ from every "
                    f"verified release; fingerprint {found})"
                )
            )
    return _VERDICT["verdict"]


def session_wanted() -> bool:
    """Return the session setting as role_var reads it (environment, then ansible.cfg)."""
    value = os.environ.get(SWITCH)
    if value is None and constants.CONFIG_FILE:
        parser = configparser.ConfigParser()
        try:
            parser.read(constants.CONFIG_FILE)
            value = parser.get(SECTION, "session", fallback=None)
        except configparser.Error:
            return False
    if value is None:
        return True
    return bool(boolean(value, strict=False))


def announce() -> None:
    """Say in the controller, once, whether the session may run on the installed release."""
    global ANNOUNCED
    if ANNOUNCED or not session_wanted():
        return
    ANNOUNCED = True
    reason = unverified()
    if reason is None:
        found = str(_VERDICT["fingerprint"])
        display.vv(
            f"{PREFIX} Resolver session on: the internals of ansible-core "
            f"{ANSIBLE_CORE_VERSION} it relies on match a release it was "
            f"{VERIFIED_FINGERPRINTS[found]} (fingerprint {found[:16]})"
        )
        return
    where = constants.CONFIG_FILE or "ansible.cfg"
    display.display(
        f"{PREFIX} Resolver session off: {reason}. role_var, role_web and docker_vars render "
        "every reference separately (slower, same results). To hide this line, set "
        f'"session = false" in the [{SECTION}] section of {where} (or {SWITCH}=false in '
        "Ansible's environment).",
        stderr=True,
    )
