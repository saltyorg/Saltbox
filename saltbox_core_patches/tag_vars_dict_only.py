"""Tag only the deprecated `vars` dictionary, not a copy of every value in it.

For each task, VariableManager.get_vars builds the deprecated `vars` dictionary (its removal is
announced for ansible-core 2.24) holding every variable once more, and stores a deprecation-
tagged copy of each value in it, so that a template reading a value through `vars` shows the
deprecation warning. Making those copies is about half of each variable build.

This patch replaces the tag object get_vars uses there with a stand-in that tags the `vars`
dictionary itself, which is what every direct use (`vars['name']`, `vars.name`, `vars.keys()`,
`'name' in vars`) reads first, so each of them still shows the warning, from the same template
position. Values are stored as they are, except those that ansible-core has already tagged with a
deprecation of their own (top-level facts, play_hosts), which get the `vars` tag as in
ansible-core, and empty dictionaries.

Difference: a value taken out of `vars` no longer carries the deprecation itself, so only the
template line that reads `vars` warns, not later uses of the value elsewhere: a variable set from
it with set_fact or register, a host added with add_host, a later play, loop items built from
`vars` (for example `vars | dict2items`, then item.value), or a task environment. Top-level facts
keep their own repeat warning. A plugin that reads task_vars['vars'][name] in Python no longer
warns.

Switch: SALTBOX_TAG_VARS_DICT_ONLY, or tag_vars_dict_only in the [saltbox_core_patches] section
of ansible.cfg (see _gate.py).
"""

from __future__ import annotations

from typing import Any

# ansible-core ships no py.typed marker, so its imports are untyped.
from ansible.module_utils._internal._datatag._tags import (  # type: ignore[import-untyped]
    Deprecated,
)
from ansible.vars import manager  # type: ignore[import-untyped]

from ._gate import PREFIX, Target, allowed, display

PATCH = "tag_vars_dict_only"
SWITCH = "SALTBOX_TAG_VARS_DICT_ONLY"
# The ansible-core code this patch relies on: get_vars is the only user of _DEPRECATE_VARS in
# the whole package and calls only its tag(); the tag implementation and the tag test the
# stand-in uses; and the templating path that turns a read of the tagged `vars` dictionary into
# the deprecation warning.
TARGETS: tuple[Target, ...] = (
    ("vars/manager.py", "VariableManager.get_vars"),
    ("vars/manager.py", "refs:_DEPRECATE_VARS"),
    (".", "files:_DEPRECATE_VARS"),
    ("module_utils/_internal/_datatag/__init__.py", "AnsibleDatatagBase.tag"),
    ("module_utils/_internal/_datatag/__init__.py", "AnsibleDatatagBase.is_tagged_on"),
    ("module_utils/_internal/_datatag/__init__.py", "AnsibleTagHelper.tag"),
    ("module_utils/_internal/_datatag/__init__.py", "_try_get_internal_tags_mapping"),
    (
        "_internal/_templating/_lazy_containers.py",
        "_AnsibleLazyTemplateMixin._proxy_or_render_lazy_value",
    ),
    ("_internal/_templating/_access.py", "NotifiableAccessContextBase"),
    ("_internal/_templating/_access.py", "AnsibleAccessContext"),
    ("_internal/_templating/_datatag.py", "DeprecatedAccessAuditContext"),
    ("_internal/_templating/_jinja_bits.py", "_new_context"),
    ("_internal/_templating/_jinja_bits.py", "_flatten_and_lazify_vars"),
    (
        "_internal/_templating/_lazy_containers.py",
        "_AnsibleLazyTemplateMixin._try_create",
    ),
    ("_internal/_templating/_lazy_containers.py", "_AnsibleLazyTemplateDict"),
    ("_internal/_templating/_utils.py", "LazyOptions"),
)
# Releases this patch was run against.
VERIFIED_RELEASES = frozenset(
    {"2.21.2", "2.21.3", "2.21.4", "2.21.5", "2.21.6", "2.22.0"}
)
# 2.21.2 to 2.21.6 and 2.22.0 (the same source).
VERIFIED_FINGERPRINTS = frozenset(
    {
        "9e90703c8394ae4e37787bdc1d199b371aa6882beeff437537602d96db691ca4",
    }
)
INSTALLED = False


class VarsDictOnlyTag:
    """Stand-in for the Deprecated tag get_vars applies to the `vars` dictionary and its values.

    get_vars calls tag() once with the new, empty `vars` dictionary and then once per variable,
    `vars` itself included. Empty dictionaries (the new `vars` dictionary among them) and values
    that already carry a Deprecated tag (the `vars` dictionary itself, top-level facts,
    play_hosts) are tagged as by ansible-core, which also copies `vars` instead of storing it in
    itself; every other value is returned unchanged. Every other attribute is the real tag's.
    """

    def __init__(self, real: Any) -> None:
        self.real = real

    def __getattr__(self, name: str) -> Any:
        if name == "real":
            raise AttributeError(name)
        return getattr(self.real, name)

    def tag(self, value: Any) -> Any:
        """Return the value get_vars stores in the `vars` dictionary."""
        if (isinstance(value, dict) and not value) or Deprecated.is_tagged_on(value):
            return self.real.tag(value)
        return value


def install() -> bool:
    """Replace manager._DEPRECATE_VARS if the gate allows it."""
    current = getattr(manager, "_DEPRECATE_VARS", None)
    if type(current).__name__ == VarsDictOnlyTag.__name__:
        return True
    reason = allowed(PATCH, SWITCH, VERIFIED_RELEASES, VERIFIED_FINGERPRINTS, TARGETS)
    if reason is None:
        return False
    if type(current) is not Deprecated:
        display.display(
            f"{PREFIX} {PATCH}: not installed, ansible.vars.manager._DEPRECATE_VARS is "
            f"{type(current).__name__}, not Deprecated",
            stderr=True,
        )
        return False
    manager._DEPRECATE_VARS = VarsDictOnlyTag(current)
    display.vv(f"{PREFIX} {PATCH}: installed ({reason})")
    return True


INSTALLED = install()
