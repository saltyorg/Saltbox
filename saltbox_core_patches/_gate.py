"""Install gate shared by the Saltbox ansible-core patches.

A patch is installed only when its switch allows it and the running ansible-core is either a
release the patch was verified against, or a release whose source of every ansible-core function
the patch relies on is identical to a verified release (a fingerprint of the unparsed syntax
tree, see _fingerprint.py, so formatting, comments and the Python version do not count).
Otherwise ansible-core's own code runs and one line says why.

Switches, first match wins:
  1. the environment variable named by the patch (for example SALTBOX_SKIP_BOOKKEEPING_VARS);
     an empty value counts as unset;
  2. the option named after the patch in the [saltbox_core_patches] section of the ansible.cfg
     Ansible uses, for example "skip_bookkeeping_vars = false" (a ";" comment may follow the
     value). This one also reaches runs started through sb, which drops the environment when it
     re-runs itself through sudo. The section name is lower case; the option is also found in
     another letter case. The option in another section, or the section in another letter case,
     is ignored with a warning;
  3. enabled.
A value must be an Ansible boolean (yes/no, true/false, on/off, y/n, t/f, 1/0, any case). Any
other value keeps the patch off and prints a warning.
"""

from __future__ import annotations

import configparser
import os
from collections.abc import Sequence

# ansible-core ships no py.typed marker, so its imports are untyped.
import ansible  # type: ignore[import-untyped]
from ansible import constants  # type: ignore[import-untyped]
from ansible.release import __version__ as ANSIBLE_CORE_VERSION  # type: ignore[import-untyped]
from ansible.utils.display import Display  # type: ignore[import-untyped]

from ._fingerprint import Target, fingerprint

display = Display()

PREFIX = "[saltbox_core_patches]"
SECTION = "saltbox_core_patches"
ANSIBLE_ROOT = os.path.dirname(os.path.abspath(ansible.__file__))
TRUE = frozenset({"yes", "true", "on", "y", "t", "1"})
FALSE = frozenset({"no", "false", "off", "n", "f", "0"})
_CONFIG: dict[str, configparser.ConfigParser | None] = {}

__all__ = [
    "ANSIBLE_CORE_VERSION",
    "PREFIX",
    "Target",
    "allowed",
    "display",
    "fingerprint",
]


def _config_option(patch: str) -> tuple[str | None, str]:
    """Return the patch's option from Ansible's config file, and where it was looked up."""
    path = constants.CONFIG_FILE
    if not path:
        return None, "no ansible.cfg"
    if path not in _CONFIG:
        # As ansible-core reads ansible.cfg: ";" starts an inline comment.
        read = configparser.ConfigParser(
            interpolation=None, inline_comment_prefixes=(";",)
        )
        try:
            read.read(path, encoding="utf-8")
            _CONFIG[path] = read
        except (OSError, configparser.Error):
            _CONFIG[path] = None
    parser = _CONFIG[path]
    where = f"{patch} in [{SECTION}] of {path}"
    if parser is None:
        return None, where
    for section in parser.sections():
        if section != SECTION and parser.has_option(section, patch):
            display.warning(
                f"{PREFIX} {patch}: {patch} in [{section}] of {path} is ignored; the switch "
                f"is read from a [{SECTION}] section (lower case)"
            )
    if not parser.has_option(SECTION, patch):
        return None, where
    return parser.get(SECTION, patch), where


def switch(patch: str, variable: str) -> tuple[bool | None, str]:
    """Return (enabled, source); enabled is None for a value that is not a boolean."""
    value = os.environ.get(variable) or None
    source = variable
    if value is None:
        value, source = _config_option(patch)
    if value is None:
        return True, "default"
    text = value.strip().lower()
    if text in TRUE:
        return True, f"{source}={value}"
    if text in FALSE:
        return False, f"{source}={value}"
    return None, f"{source}={value!r}"


def advice(patch: str, variable: str) -> str:
    """Return how to switch the patch off."""
    where = constants.CONFIG_FILE or "ansible.cfg"
    return (
        f'set "{patch} = false" in the [{SECTION}] section of {where} '
        f"(or {variable}=false in Ansible's environment)"
    )


def allowed(
    patch: str,
    variable: str,
    releases: frozenset[str],
    fingerprints: frozenset[str],
    targets: Sequence[Target],
) -> str | None:
    """Return why the patch may be installed, or None after saying why it is not."""
    enabled, source = switch(patch, variable)
    if enabled is None:
        display.warning(
            f"{PREFIX} {patch}: not installed, {source} is not a boolean "
            "(use yes/no, true/false, on/off or 1/0)"
        )
        return None
    if not enabled:
        display.vv(f"{PREFIX} {patch}: not installed, switched off by {source}")
        return None
    if ANSIBLE_CORE_VERSION in releases:
        return f"verified ansible-core {ANSIBLE_CORE_VERSION}"
    try:
        found = fingerprint(targets, ANSIBLE_ROOT)
        shown = f"fingerprint {found[:16]}"
    except (OSError, SyntaxError, ValueError, LookupError) as ex:
        found, shown = "", f"unreadable: {ex}"
    if found in fingerprints:
        return f"ansible-core {ANSIBLE_CORE_VERSION} source matches a verified release"
    display.display(
        f"{PREFIX} {patch}: not installed, ansible-core {ANSIBLE_CORE_VERSION} is not a "
        f"verified release and the source it relies on differs ({shown}); ansible-core's "
        f"own code runs. To hide this line, {advice(patch, variable)}.",
        stderr=True,
    )
    return None
