"""Reuse the lazy wrappers of a large variable dictionary across the templates that read it.

Each time ansible-core renders a template, _new_context gives Jinja a fresh lazy copy of every
variable layer (_flatten_and_lazify_vars): a new dictionary in which every value is wrapped in a
_LazyValue, so that a value is templated only when the template reads it. For a task's variables
(about 3,400 in Saltbox and 10,000 in Sandbox) that is thousands of new objects per render, many
renders per task, and the garbage collector work those objects cause.

The wrappers themselves never change: a _LazyValue is a frozen holder of one value, and a lazy
dictionary stores a rendered value by replacing the wrapper in its own storage. This patch keeps
the wrapped items of the last few large layers (dict or tagged dict, at least MIN_ITEMS keys) and
builds each render's lazy dictionary as a plain copy of them, with the current templar and
options as before. Kept wrappers are used only for keys the layer still has, in the same order,
holding the same value objects (compared by identity, not equality). When values or keys change
in place (a loop variable, the recursion guard a Saltbox lookup adds and removes), the changed
values get new wrappers; the last few states of a layer are kept. So every render still gets its
own fresh lazy dictionary with the same keys, order, tags and values as without the patch, and
renders nothing more or less; rendered results are never shared between renders.

Memory: a kept state holds the values it wrapped, so a value that a layer no longer holds (a
replaced loop variable or registered result, or a whole earlier task's variables in the
controller) stays in memory until its state is dropped: after STATES newer states of the same
layer, or once KEEP other large layers have been lazified since. At most KEEP x STATES states are
kept per process; task workers exit after their task.

The patch relies on a _LazyValue never being changed or compared by identity once made; the
fingerprint covers every ansible-core module that mentions it.

Switch: SALTBOX_SHARE_LAZY_VAR_WRAPPERS, or share_lazy_var_wrappers in the
[saltbox_core_patches] section of ansible.cfg (see _gate.py).
"""

from __future__ import annotations

import itertools
import operator
from collections import ChainMap
from collections.abc import Iterator, Mapping
from typing import Any

# ansible-core ships no py.typed marker, so its imports are untyped.
from ansible._internal._templating import _jinja_bits  # type: ignore[import-untyped]
from ansible._internal._templating._lazy_containers import (  # type: ignore[import-untyped]
    _AnsibleLazyTemplateDict,
    _AnsibleLazyTemplateMixin,
    _LazyValue,
    _LazyValueSource,
)
from ansible._internal._templating._utils import (  # type: ignore[import-untyped]
    LazyOptions,
    TemplateContext,
)
from ansible.module_utils._internal._datatag import (  # type: ignore[import-untyped]
    _AnsibleTaggedDict,
    _try_get_internal_tags_mapping,
)

from ._gate import PREFIX, Target, allowed, display

PATCH = "share_lazy_var_wrappers"
SWITCH = "SALTBOX_SHARE_LAZY_VAR_WRAPPERS"
# The ansible-core code this patch relies on: the replaced function and its only caller, how
# _try_create lazifies a dictionary layer (dispatch, wrapper, value source, tags, instance
# factory), how the lazy dictionary consumes the wrappers, and every module and function that
# uses _LazyValue (so new code that changes or identity-compares wrappers is noticed).
TARGETS: tuple[Target, ...] = (
    ("_internal/_templating/_jinja_bits.py", "_flatten_and_lazify_vars"),
    ("_internal/_templating/_jinja_bits.py", "_new_context"),
    ("_internal/_templating/_jinja_bits.py", "refs:_flatten_and_lazify_vars"),
    (".", "files:_flatten_and_lazify_vars"),
    ("_internal/_templating/_lazy_containers.py", "_LazyValue"),
    ("_internal/_templating/_lazy_containers.py", "_LazyValueSource"),
    ("_internal/_templating/_lazy_containers.py", "_AnsibleLazyTemplateMixin"),
    ("_internal/_templating/_lazy_containers.py", "_AnsibleLazyTemplateDict"),
    ("_internal/_templating/_utils.py", "LazyOptions"),
    ("_internal/_templating/_utils.py", "TemplateContext"),
    ("module_utils/_internal/_datatag/__init__.py", "_try_get_internal_tags_mapping"),
    ("module_utils/_internal/_datatag/__init__.py", "_AnsibleTaggedDict"),
    ("module_utils/_internal/_datatag/__init__.py", "AnsibleTaggedObject"),
    (".", "files:_LazyValue"),
    ("_internal/_templating/_lazy_containers.py", "refs:_LazyValue"),
    ("_internal/_templating/_lazy_containers.py", "_AnsibleLazyTemplateList"),
)
# Releases this patch was run against.
VERIFIED_RELEASES = frozenset({"2.21.2", "2.22.0"})
# 2.21.2 and 2.22.0 (the same source).
VERIFIED_FINGERPRINTS = frozenset(
    {
        "5ad3ece6dd40320c8e1256e0c6d62ec5627c6f821cf250005e5451da69722cd4",
    }
)
# Smaller layers (task vars:, Jinja locals, loop scopes) are lazified as before.
MIN_ITEMS = 256
# Layer objects kept per process; a task's templates read one or two large layers.
KEEP = 4
# States kept per layer: lookups add and remove their recursion guard in place.
STATES = 4
# A state is derived from the last one only while at most 1 in DERIVE_LIMIT values changed.
DERIVE_LIMIT = 8
MARKER = "_saltbox_share_lazy_var_wrappers"
STATS: dict[str, int] = {"reused": 0, "derived": 0, "built": 0, "small": 0}
INSTALLED = False

_LAZY_DICT_TYPES = (dict, _AnsibleTaggedDict)


class _Kept:
    """The wrapped items of one state of a layer and the exact keys and values they wrap."""

    __slots__ = ("keys", "values", "wrapped")

    def __init__(
        self,
        keys: tuple[Any, ...],
        values: tuple[Any, ...],
        wrapped: dict[Any, _LazyValue],
    ) -> None:
        self.keys = keys
        self.values = values
        self.wrapped = wrapped

    def matches(self, layer: dict[Any, Any]) -> bool:
        """Return whether the layer holds exactly these keys and value objects, in this order."""
        return (
            len(layer) == len(self.keys)
            and all(map(operator.is_, dict.keys(layer), self.keys))
            and all(map(operator.is_, dict.values(layer), self.values))
        )


def _build(layer: dict[Any, Any]) -> _Kept:
    """Wrap every value of the layer, as _AnsibleLazyTemplateDict._lazy_values does."""
    keys = tuple(dict.keys(layer))
    values = tuple(dict.values(layer))
    wrapped = {key: _LazyValue(value) for key, value in zip(keys, values, strict=True)}
    return _Kept(keys, values, wrapped)


def _derive(base: _Kept, layer: dict[Any, Any]) -> _Kept | None:
    """Wrap the layer from a kept state whose keys are its first keys, in the same order.

    The kept wrappers are reused for values that are the same objects; only the other values (a
    loop variable set in place) and the keys after the kept ones (a lookup's recursion guard) get
    new wrappers, so the result equals _build(layer). None when the keys differ otherwise or too
    many values changed.
    """
    count = len(base.keys)
    if len(layer) < count:
        return None
    keys = tuple(dict.keys(layer))
    if not all(map(operator.is_, keys, base.keys)):
        return None
    values = tuple(dict.values(layer))
    changed = list(
        itertools.compress(range(count), map(operator.is_not, values, base.values))
    )
    if len(changed) * DERIVE_LIMIT > count:
        return None
    wrapped = dict(base.wrapped)
    for index in changed:
        wrapped[keys[index]] = _LazyValue(values[index])
    for index in range(count, len(keys)):
        wrapped[keys[index]] = _LazyValue(values[index])
    return _Kept(keys, values, wrapped)


class _Layer:
    """The kept states of one layer object, most recently used first."""

    __slots__ = ("layer", "states")

    def __init__(self, layer: dict[Any, Any]) -> None:
        self.layer = layer
        self.states: list[_Kept] = []


_KEPT: dict[int, _Layer] = {}


def _entry(layer: dict[Any, Any]) -> _Layer:
    """Return the kept states of this layer object, starting a new entry if needed."""
    entry = _KEPT.get(id(layer))
    if entry is not None and entry.layer is layer:
        return entry
    entry = _Layer(layer)
    _KEPT.pop(id(layer), None)
    _KEPT[id(layer)] = entry
    while len(_KEPT) > KEEP:
        try:
            _KEPT.pop(next(iter(_KEPT)), None)
        except (RuntimeError, StopIteration):  # changed by another thread meanwhile
            break
    return entry


def _wrapped_items(layer: dict[Any, Any]) -> dict[Any, _LazyValue]:
    """Return wrapped items for the layer's current keys and values, reusing kept wrappers."""
    states = _entry(layer).states
    for index, state in enumerate(states):
        if state.matches(layer):
            STATS["reused"] += 1
            if index:
                states.insert(0, states.pop(index))
            return state.wrapped
    derived = _derive(states[0], layer) if states else None
    if derived is None:
        STATS["built"] += 1
        derived = _build(layer)
    else:
        STATS["derived"] += 1
    states.insert(0, derived)
    del states[STATES:]
    return derived.wrapped


def _lazify_layer(layer: dict[Any, Any]) -> Any:
    """What _AnsibleLazyTemplateMixin._try_create(layer) returns, from reused wrappers."""
    source = _LazyValueSource(
        source=_wrapped_items(layer),
        templar=TemplateContext.current().templar,
        lazy_options=LazyOptions.DEFAULT,
    )
    return _AnsibleLazyTemplateDict._instance_factory(
        source, _try_get_internal_tags_mapping(layer)
    )


def _flatten_and_lazify_vars(mapping: Mapping[Any, Any]) -> Iterator[Mapping[Any, Any]]:
    """ansible-core's _flatten_and_lazify_vars, with reused wrappers for large layers."""
    mapping_type = type(mapping)
    if mapping_type is ChainMap:
        for layer in mapping.maps:  # type: ignore[attr-defined]
            yield from _flatten_and_lazify_vars(layer)
    elif mapping_type is _AnsibleLazyTemplateDict:
        yield mapping
    elif mapping_type in _LAZY_DICT_TYPES:
        if mapping:
            if len(mapping) >= MIN_ITEMS:
                yield _lazify_layer(mapping)  # type: ignore[arg-type]
            else:
                STATS["small"] += 1
                yield _AnsibleLazyTemplateMixin._try_create(mapping)
    else:
        raise NotImplementedError(
            f"unsupported mapping type in Jinja vars: {mapping_type}"
        )


setattr(_flatten_and_lazify_vars, MARKER, True)


def install() -> bool:
    """Replace _jinja_bits._flatten_and_lazify_vars if the gate allows it."""
    if getattr(_jinja_bits._flatten_and_lazify_vars, MARKER, False):
        return True
    reason = allowed(PATCH, SWITCH, VERIFIED_RELEASES, VERIFIED_FINGERPRINTS, TARGETS)
    if reason is None:
        return False
    dispatch = _AnsibleLazyTemplateMixin._dispatch_types
    if any(
        dispatch.get(kind) is not _AnsibleLazyTemplateDict for kind in _LAZY_DICT_TYPES
    ):
        display.display(
            f"{PREFIX} {PATCH}: not installed, ansible-core does not lazify dict layers "
            "as _AnsibleLazyTemplateDict",
            stderr=True,
        )
        return False
    _jinja_bits._flatten_and_lazify_vars = _flatten_and_lazify_vars
    display.vv(f"{PREFIX} {PATCH}: installed ({reason})")
    return True


INSTALLED = install()
