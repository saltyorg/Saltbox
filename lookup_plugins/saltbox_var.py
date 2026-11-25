from __future__ import annotations

DOCUMENTATION = """
    name: saltbox_var
    description:
      - Returns the value of a variable by name, exactly like referencing the variable directly in
        the template that calls it, but rendered inside the resolver session that role_var, role_web,
        docker_vars and saltbox_var share during one template evaluation.
      - The session never changes a result, a message or a warning. Each render inside it sees the
        variables exactly as they are at that moment, as without a session; what the session saves
        is the cost of preparing the variables for every render and of rendering the same variable
        again when nothing it depends on can have changed.
      - A render is reused only within the same template evaluation, for the same variable mapping
        and recursion-guard state, and only when everything it used is known to give the same result
        every time (filters, tests, lookups and functions on a fixed list; anything else, such as
        password, random, now, pipe, file, env or url, always runs again). Messages the first render
        showed are shown again on every reuse, in the same order.
      - Without a session (switched off, or an untested ansible-core version) every render runs
        exactly as before the session existed.
      - Jinja resolves every variable name a template uses once, when the template starts, even
        in branches it does not take. To get exactly that from this lookup, call it once per name
        in a C({% set %}) at the start of the template and use only that binding. Error messages
        then quote that template text instead of the direct reference.
      - Variables that only reference role variables through role_var, role_web or docker_vars
        need no saltbox_var; their lookups share the session either way.
    author: salty
    options:
      _terms:
        description: Variable names to resolve.
        required: true
      default:
        description: Value returned for a variable that is not defined.
        type: raw
        required: false
      session:
        description:
          - Configuration only, read once per process from the environment or ansible.cfg; it is not
            a lookup argument.
          - Whether role_var, role_web, docker_vars and saltbox_var share a resolver session per
            template evaluation. Set to false to render every reference separately.
          - The ansible.cfg setting applies to playbooks run with that ansible.cfg (Saltbox, Sandbox
            and saltbox_mod each have their own); the environment variable applies everywhere.
        type: bool
        default: true
        env:
          - name: SALTBOX_LOOKUP_SESSION
        ini:
          - section: saltbox_lookups
            key: session
"""

EXAMPLES = """
- name: Use the shared Docker labels inside the resolver session
  debug:
    msg: "{{ lookup('saltbox_var', 'docker_labels_common') }}"
"""

RETURN = """
  _raw:
    description: The value of each named variable.
    type: list
"""

import copy
import os
import sys
import threading
import weakref
from typing import Any

from ansible import constants as C  # type: ignore[import-untyped]
from ansible.errors import AnsibleLookupError  # type: ignore[import-untyped]
from ansible.module_utils.ansible_release import __version__ as _CORE_VERSION  # type: ignore[import-untyped]
from ansible.plugins.lookup import LookupBase  # type: ignore[import-untyped]
from ansible.utils.display import Display  # type: ignore[import-untyped]

display = Display()

# The session builds on ansible-core internals, so it only runs on versions it was tested with.
# Any other version, a missing internal, or a failure while creating a session switches it off
# for the rest of the process (with a warning) and every lookup takes its plain path.
_TESTED_CORE_VERSIONS = frozenset({"2.21.2"})

# Keys role_var and docker_vars keep their recursion guards under in the variable mapping.
_STACK_KEYS = ("__saltbox_role_var_stack__", "__saltbox_docker_vars_stack__")

# A render is reused only when everything it used is listed here. Each entry is deterministic:
# same arguments and variables, same result, no output of its own and nothing read from outside
# the variables (files, environment, clock, randomness, network).
_PURE_LOOKUPS = frozenset(
    {
        "role_var",
        "role_web",
        "docker_vars",
        "saltbox_var",
        "vars",
        "ansible.builtin.vars",
        "varnames",
        "ansible.builtin.varnames",
    }
)
_PURE_FILTERS = frozenset(
    {
        # Jinja
        "abs", "attr", "batch", "capitalize", "center", "count", "d", "default", "dictsort",
        "e", "escape", "filesizeformat", "first", "float", "forceescape", "format", "groupby",
        "indent", "int", "items", "join", "last", "length", "list", "lower", "map", "max",
        "min", "pprint", "reject", "rejectattr", "replace", "reverse", "round", "safe",
        "select", "selectattr", "slice", "sort", "string", "striptags", "sum", "title",
        "tojson", "trim", "truncate", "unique", "upper", "urlencode", "urlize", "wordcount",
        "wordwrap", "xmlattr",
        # ansible.builtin
        "b64decode", "b64encode", "basename", "bool", "checksum", "combinations", "combine",
        "comment", "commonpath", "dict2items", "difference", "dirname", "extract", "flatten",
        "from_json", "from_yaml", "from_yaml_all", "hash", "human_readable", "human_to_bytes",
        "intersect", "items2dict", "log", "mandatory", "md5", "normpath", "path_join",
        "permutations", "pow", "product", "quote", "regex_escape", "regex_findall",
        "regex_replace", "regex_search", "root", "sha1", "split", "splitext", "subelements",
        "symmetric_difference", "ternary", "to_datetime", "to_json", "to_nice_json",
        "to_nice_yaml", "to_uuid", "to_yaml", "type_debug", "union", "urldecode", "urlsplit",
        "win_basename", "win_dirname", "win_splitdrive", "zip", "zip_longest",
        # community.general
        "community.general.dict_kv", "community.general.json_query",
        "community.general.from_toml", "json_query",
        # Saltbox filter_plugins
        "saltbox_argv", "saltbox_ip_address", "traefik_host_rule", "traefik_certificate_labels",
        "traefik_certificate_plan", "traefik_certificate_domains", "tld_parse",
        "filter_rclone_remote_name", "filter_rclone_remote_with_path",
        "filter_rclone_first_remote_name", "filter_rclone_first_remote_name_with_path",
    }
)  # fmt: skip
_PURE_TESTS = frozenset(
    {
        # Jinja
        "boolean", "callable", "defined", "divisibleby", "eq", "equalto", "escaped", "even",
        "false", "filter", "float", "ge", "greaterthan", "gt", "in", "integer", "iterable",
        "le", "lessthan", "lower", "lt", "mapping", "ne", "none", "number", "odd", "sameas",
        "sequence", "string", "test", "true", "undefined", "upper", "==", "!=", ">", ">=",
        "<", "<=",
        # ansible.builtin
        "abs", "all", "any", "change", "changed", "contains", "failed", "failure", "falsy",
        "finished", "match", "nan", "reachable", "regex", "search", "skip", "skipped",
        "started", "subset", "succeeded", "success", "superset", "timedout", "truthy",
        "unreachable", "uri", "url", "urn", "vault_encrypted", "version", "version_compare",
    }
)  # fmt: skip
_PURE_CALLS = frozenset(
    {
        "range",
        "dict",
        "lookup",
        "query",
        "q",
        "undef",
        "namespace",
        "joiner",
        "cycler",
        "caller",
    }
)
_FQCN_PREFIXES = ("ansible.builtin.", "ansible.legacy.")

# Display calls a reused render can repeat exactly; any other display call during a render (for
# example a deprecation, whose reporter is found from the call stack) means it is not reused.
_REPLAYABLE = frozenset(
    {"display", "verbose", "v", "vv", "vvv", "vvvv", "vvvvv", "vvvvvv", "warning"}
)
_HOOKED = (
    *_REPLAYABLE,
    "debug",
    "deprecated",
    "_deprecated_with_plugin_info",
    "_deprecated",
    "_warning",
    "error",
    "_error",
    "error_as_warning",
    "banner",
    "banner_cowsay",
    "system_warning",
    "_verbose_display",
    "_verbose_log",
)

_SessionEngine: Any = None

try:
    from ansible._internal._templating import _jinja_plugins  # type: ignore[import-untyped]
    from ansible._internal._templating._engine import (  # type: ignore[import-untyped]
        TemplateEngine,
        TemplateMode,
        TemplateOptions,
    )
    from ansible._internal._templating._jinja_bits import _TemplateCompileContext  # type: ignore[import-untyped]
    from ansible._internal._templating._jinja_common import UndefinedMarker  # type: ignore[import-untyped]
    from ansible._internal._templating._lazy_containers import (  # type: ignore[import-untyped]
        _AnsibleLazyTemplateDict,
        _AnsibleLazyTemplateMixin,
        _LazyValue,
    )
    from ansible._internal._templating._utils import LazyOptions, Omit, TemplateContext  # type: ignore[import-untyped]
    from ansible.module_utils._internal import _traceback  # type: ignore[import-untyped]
    from ansible.module_utils._internal._datatag import (  # type: ignore[import-untyped]
        _ANSIBLE_ALLOWED_NON_SCALAR_COLLECTION_VAR_TYPES,
        _ANSIBLE_ALLOWED_SCALAR_VAR_TYPES,
        AnsibleTagHelper,
        _AnsibleTaggedDict,
        _AnsibleTaggedStr,
        _try_get_internal_tags_mapping,
    )
    from ansible.template import Templar  # type: ignore[import-untyped]
    from jinja2 import nodes
except ImportError as _error:
    _UNSUPPORTED: str | None = f"ansible-core internals unavailable ({_error})"
else:
    _UNSUPPORTED = None
    if _CORE_VERSION not in _TESTED_CORE_VERSIONS:
        _UNSUPPORTED = f"ansible-core {_CORE_VERSION} is untested"
    elif not (
        hasattr(Templar, "_from_template_engine")
        and hasattr(_AnsibleLazyTemplateMixin, "_try_create")
        and "_templar" in _AnsibleLazyTemplateMixin._SLOTS
        and hasattr(TemplateEngine, "_compile_template")
        and hasattr(TemplateEngine, "_create_overlay")
        and isinstance(getattr(TemplateEngine, "available_variables", None), property)
        and callable(getattr(_jinja_plugins, "_invoke_lookup", None))
        and TemplateContext.__dictoffset__ != 0
        and all(callable(getattr(display, name, None)) for name in _HOOKED)
    ):
        _UNSUPPORTED = "ansible-core internals changed"
    else:
        _INVOKE_LOOKUP_FILE = _jinja_plugins.__file__
        # Display calls repeated on reuse come from these files only (see _note_display).
        _REPLAY_SOURCES = frozenset(
            {
                _INVOKE_LOOKUP_FILE,
                __file__,
                *(
                    os.path.join(os.path.dirname(__file__), f"{name}.py")
                    for name in ("role_var", "docker_vars", "role_web")
                ),
            }
        )
        _BASE_AVAILABLE_VARIABLES = TemplateEngine.available_variables
        _STR_TYPES = frozenset({str, _AnsibleTaggedStr})
        _SCALAR_TYPES = frozenset({*_ANSIBLE_ALLOWED_SCALAR_VAR_TYPES, type(Omit)})
        _COLLECTION_TYPES = frozenset(_ANSIBLE_ALLOWED_NON_SCALAR_COLLECTION_VAR_TYPES)
        _MAPPING_TYPES = frozenset(
            item for item in _COLLECTION_TYPES if issubclass(item, dict)
        )

        class _Engine(TemplateEngine):
            """Template engine of one session.

            Each render through it gets its own copy of the session's lazy view of the variables,
            as each render gets its own lazy view without a session; the copy is cheap because the
            view is prepared once. A lookup plugin called from a render gets the session's plain
            mapping, the same object it gets without a session.
            """

            _saltbox_session: Any = None

            def __deepcopy__(self, memo: dict[int, Any]) -> Any:
                # A deep copy (for example of a lazy value, made by a filter) is what it is without
                # a session: an engine holding a deep copy of the variables.
                state = dict(self.__dict__)
                session = state.pop("_saltbox_session", None)
                if session is not None and state.get("_variables") is None:
                    state["_variables"] = session.root
                    state["_variables_factory"] = None
                engine = TemplateEngine.__new__(TemplateEngine)
                memo[id(self)] = engine
                engine.__dict__.update(copy.deepcopy(state, memo))
                return engine

            def extend(self, marker_behavior: Any = None, variables: Any = None) -> Any:
                engine = super().extend(
                    marker_behavior=marker_behavior, variables=variables
                )
                if variables is None:
                    engine._saltbox_session = self._saltbox_session
                return engine

            @property
            def available_variables(self) -> Any:
                session = self._saltbox_session
                if session is None:
                    return _BASE_AVAILABLE_VARIABLES.fget(self)
                caller = sys._getframe(1).f_code
                if (
                    caller.co_name == "_invoke_lookup"
                    and caller.co_filename == _INVOKE_LOOKUP_FILE
                ):
                    return session.root
                return session.fresh_view(self)

            @available_variables.setter
            def available_variables(self, variables: Any) -> None:
                # Variables set from outside (a lookup on its own templar) replace the session's.
                self._saltbox_session = None
                _BASE_AVAILABLE_VARIABLES.fset(self, variables)

            def template(
                self,
                variable: Any,
                *,
                options: Any = TemplateOptions.DEFAULT,
                mode: Any = TemplateMode.DEFAULT,
                lazy_options: Any = LazyOptions.DEFAULT,
            ) -> Any:
                session = self._saltbox_session
                if session is None or type(variable) not in _STR_TYPES:
                    return super().template(
                        variable, options=options, mode=mode, lazy_options=lazy_options
                    )
                return session.render(self, variable, options, mode, lazy_options)

            def _compile_template(self, template: str, options: Any) -> Any:
                frames = _frames()
                if self._saltbox_session is None or not frames:
                    return super()._compile_template(template, options)
                frame = frames[-1]
                frame.compiled = True
                try:
                    key: Any = (template, options.overrides)
                    pure = _PURITY.get(key)
                except TypeError:
                    key = None
                    pure = False
                if pure is None:
                    try:
                        stripped, env = self._create_overlay(
                            template, options.overrides
                        )
                        with _TemplateCompileContext(
                            escape_backslashes=options.escape_backslashes
                        ):
                            tree = env._parse(stripped, None, None)
                            pure = _pure_tree(tree)
                            compiled = env.from_string(tree)
                    except Exception:  # noqa: BLE001 - compile again the ordinary way
                        pure = False
                        compiled = super()._compile_template(template, options)
                    if key is not None:
                        _PURITY[key] = pure
                    if not pure:
                        frame.pure = False
                    return compiled
                if not pure:
                    frame.pure = False
                return super()._compile_template(template, options)

            def _compile_expression(self, expression: str, options: Any) -> Any:
                frames = _frames()
                if self._saltbox_session is not None and frames:
                    frames[-1].pure = False
                return super()._compile_expression(expression, options)

        _SessionEngine = _Engine

_enabled: bool | None = None
_TLS = threading.local()

# Purity of template sources seen in this process, by (source, overrides).
_PURITY: dict[Any, bool] = {}

# Changes whenever a lookup that is not on the pure list runs; such a lookup could change the
# variable mapping it was given, so sessions check their prepared view against it afterwards.
_EPOCH = 0


def _frames() -> list[_Frame]:
    frames: list[_Frame] | None = getattr(_TLS, "frames", None)
    if frames is None:
        frames = _TLS.frames = []
    return frames


def _switch_off(reason: str, notice: str = "warning") -> None:
    """Switch sessions off for the rest of the process and say why.

    notice: "warning" for an internal error on a tested ansible-core version; "none" for an
    untested version or the session setting (output then stays exactly as without the session).
    """
    global _enabled
    _enabled = False
    message = (
        f"[saltbox_var] Resolver session off: {reason}. role_var, role_web and docker_vars "
        "render every reference separately (slower, same results)."
    )
    if notice == "warning":
        display.warning(message)
    elif notice == "vvvv":
        display.vvvv(message)


def _session_enabled() -> bool:
    """Return whether sessions are used in this process, deciding once."""
    global _enabled
    if _enabled is None:
        if _UNSUPPORTED is not None:
            _switch_off(_UNSUPPORTED, notice="none")
        else:
            try:
                switch = C.config.get_config_value(
                    "session", plugin_type="lookup", plugin_name="saltbox_var"
                )
            except Exception as error:  # noqa: BLE001 - unreadable means off
                _switch_off(f"session setting unreadable ({error})")
            else:
                if not switch:
                    _switch_off("switched off by the session setting", notice="none")
                else:
                    try:
                        _install_hooks()
                    except Exception as error:  # noqa: BLE001 - internals changed
                        _switch_off(f"internal error while installing hooks ({error})")
                    else:
                        _enabled = True
    return bool(_enabled)


class _Frame:
    """Record of one render through a session engine.

    pure      every filter, test, lookup and function it used is on the pure lists and every
              display call it made can be repeated
    compiled  it rendered an actual template
    log       display calls made during the render, in order, nested renders included
    guards    recursion-guard checks whose outcome depends on guards pushed before the render
    bases     (guard list, length) of each recursion-guard key when the render started
    """

    __slots__ = ("bases", "compiled", "guards", "log", "pure")

    def __init__(self, bases: tuple[Any, ...]) -> None:
        self.pure = True
        self.compiled = False
        self.log: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.guards: list[tuple[str, str, bool]] = []
        self.bases = bases


def _normalise(name: Any) -> Any:
    if isinstance(name, str):
        for prefix in _FQCN_PREFIXES:
            if name.startswith(prefix):
                return name[len(prefix) :]
    return name


def _const_name(node: Any) -> str | None:
    if isinstance(node, nodes.Const) and isinstance(node.value, str):
        return _normalise(node.value)
    return None


# Filters that call another filter or test named by their arguments.
_DISPATCHING_FILTERS = frozenset(
    {"map", "select", "reject", "selectattr", "rejectattr"}
)


def _pure_dispatch(name: str, node: Any) -> bool:
    """Return whether a map/select-style filter only names pure filters or tests."""
    if node.dyn_args is not None or node.dyn_kwargs is not None:
        return False
    args = node.args
    if name == "map":
        return not args or _const_name(args[0]) in _PURE_FILTERS
    if name in ("select", "reject"):
        return not args or _const_name(args[0]) in _PURE_TESTS
    return len(args) < 2 or _const_name(args[1]) in _PURE_TESTS


def _pure_tree(tree: Any) -> bool:
    """Return whether a parsed template uses only pure filters, tests and calls."""
    macros = {node.name for node in tree.find_all(nodes.Macro)}
    for node in tree.find_all(nodes.Node):
        kind = type(node)
        if kind is nodes.Filter:
            name = _normalise(node.name)
            if name not in _PURE_FILTERS:
                return False
            if name in _DISPATCHING_FILTERS and not _pure_dispatch(name, node):
                return False
        elif kind is nodes.Test:
            if _normalise(node.name) not in _PURE_TESTS:
                return False
        elif kind is nodes.Call:
            target = node.node
            if isinstance(target, nodes.Name):
                if target.name not in _PURE_CALLS and target.name not in macros:
                    return False
            elif not isinstance(target, nodes.Getattr):
                # Methods of template values (str, dict, list) are deterministic; anything else
                # called (a value from a subscript or a call result) is not checked.
                return False
        elif kind is nodes.Name and node.name in _STACK_READERS:
            # The guard lists change while role_var and docker_vars work; a render that reads
            # them (or everything, through the deprecated `vars`) depends on their contents.
            return False
        elif kind in _IMPURE_NODES:
            return False
    return True


_STACK_READERS = frozenset({*_STACK_KEYS, "vars"})


_IMPURE_NODES = frozenset(
    getattr(nodes, name)
    for name in (
        "Include",
        "Import",
        "FromImport",
        "Extends",
        "Block",
        "ExprStmt",
        "CallBlock",
        "EvalContextModifier",
        "ScopedEvalContextModifier",
        "ExtensionAttribute",
        "ImportedName",
        "InternalName",
        "ContextReference",
        "DerivedContextReference",
        "OverlayScope",
    )
    if hasattr(nodes, name)
)


# Verbosity level below which a verbose-family call shows nothing (Display.verbose's caplevel).
_CAPLEVELS = {
    "v": 0,
    "vv": 1,
    "vvv": 2,
    "vvvv": 3,
    "vvvvv": 4,
    "vvvvvv": 5,
    "verbose": 2,
}


def _note_display(
    frame: _Frame,
    name: str,
    replayable: bool,
    caplevel: int | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    """Record a display call made during a session render, or mark the render not reusable.

    A call that shows and logs nothing at the current verbosity is ignored (repeating it would
    show nothing either). A call is repeated on reuse only when it comes from the Saltbox lookups
    or from ansible-core's lookup invoker (errors='ignore'/'warn' messages); any other source
    (a filter, the plugin loader, ansible-core's own deprecations) makes the render not reusable,
    because repeating its message later need not match what running it again would show.
    """
    if caplevel is not None:
        if name == "verbose":
            caplevel = kwargs.get("caplevel", args[2] if len(args) > 2 else 2)
        if not (
            display.verbosity > caplevel
            or (
                display.log_verbosity > display.verbosity
                and display.log_verbosity > caplevel
            )
        ):
            return
    elif name == "debug" and not C.DEFAULT_DEBUG:
        return
    if (
        replayable
        and sys._getframe(2).f_code.co_filename in _REPLAY_SOURCES
        and not (
            name == "warning"
            and _traceback.is_traceback_enabled(_traceback.TracebackEvent.WARNING)
        )
    ):
        frame.log.append((name, args, kwargs))
    else:
        frame.pure = False


def _install_hooks() -> None:
    """Record display calls made during session renders, and watch impure lookups."""
    if getattr(display, "_saltbox_session_hooks", False):
        return
    depth = threading.local()

    def hook(name: str, original: Any) -> Any:
        replayable = name in _REPLAYABLE
        caplevel = _CAPLEVELS.get(name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _skip_stackwalk = True  # ansible-core's caller detection skips this frame
            level = getattr(depth, "level", 0)
            if level == 0:
                frames = _frames()
                if frames:
                    _note_display(frames[-1], name, replayable, caplevel, args, kwargs)
            depth.level = level + 1
            try:
                return original(*args, **kwargs)
            finally:
                depth.level = level

        wrapper.__wrapped__ = original  # type: ignore[attr-defined]
        return wrapper

    for name in _HOOKED:
        setattr(display, name, hook(name, getattr(display, name)))
    display._saltbox_session_hooks = True  # type: ignore[attr-defined]

    original_invoke = _jinja_plugins._invoke_lookup

    def invoke_lookup(*args: Any, **kwargs: Any) -> Any:
        _skip_stackwalk = True  # ansible-core's caller detection skips this frame
        global _EPOCH
        try:
            pure = _normalise(kwargs.get("plugin_name")) in _PURE_LOOKUPS
        except TypeError:
            pure = False
        if pure and not _reads_stack(kwargs):
            return original_invoke(*args, **kwargs)
        if pure:
            frames = _frames()
            if frames:
                frames[-1].pure = False
            return original_invoke(*args, **kwargs)
        _EPOCH += 1
        frames = _frames()
        if frames:
            frames[-1].pure = False
        try:
            return original_invoke(*args, **kwargs)
        finally:
            _EPOCH += 1

    _jinja_plugins._invoke_lookup = invoke_lookup


def _reads_stack(kwargs: dict[str, Any]) -> bool:
    """Return whether a vars lookup call names a recursion-guard key (its contents vary)."""
    if _normalise(kwargs.get("plugin_name")) != "vars":
        return False
    terms = kwargs.get("lookup_terms") or ()
    return any(
        isinstance(term, str) and term.startswith("__saltbox_") for term in terms
    )


def _cacheable(value: Any) -> bool:
    """Return whether a render result is plain data (no lazy parts, markers or objects)."""
    pending = [value]
    budget = 20000
    while pending:
        item = pending.pop()
        kind = type(item)
        if kind in _SCALAR_TYPES:
            continue
        if kind not in _COLLECTION_TYPES:
            return False
        budget -= len(item)
        if budget < 0:
            return False
        if kind in _MAPPING_TYPES:
            pending.extend(item.keys())
            pending.extend(item.values())
        else:
            pending.extend(item)
    return True


def _compares_identity(ctx: Any) -> bool:
    """Return whether a template being rendered around this point uses the sameas test.

    A reused result is the object the first render returned, where rendering again may build a
    new one; `is sameas` is the only template construct that can tell, so no render is reused
    while any enclosing template mentions it.
    """
    while ctx is not None:
        value = ctx.template_value
        if type(value) in _STR_TYPES and "sameas" in value:
            return True
        ctx = ctx._parent_ctx
    return False


def _fresh(value: Any) -> Any:
    """Return a value equal to ``value`` that shares no container with it, tags included."""
    if type(value) in _SCALAR_TYPES:
        return value
    return copy.deepcopy(value)


class _State:
    """What a session prepares for one variable mapping.

    pristine  lazy view of the mapping (recursion-guard keys left out), never rendered into
    size      number of keys of the pristine view
    cache     reusable renders: key -> (source, value, display log, guard conditions)
    children  states for the mapping with one <role>_name replaced (role_var instance=)
    epoch     value of _EPOCH when the pristine view was last checked against the mapping
    """

    __slots__ = ("cache", "children", "epoch", "pristine", "size")

    def __init__(self) -> None:
        self.pristine: Any = None
        self.size = 0
        self.cache: dict[Any, tuple[Any, Any, tuple[Any, ...], tuple[Any, ...]]] = {}
        self.children: dict[Any, _State] = {}
        self.epoch = _EPOCH

    def reset(self) -> None:
        self.pristine = None
        self.size = 0
        self.cache.clear()
        self.children.clear()
        self.epoch = _EPOCH

    def matches(self, root: dict[str, Any]) -> bool:
        """Return whether the pristine view still holds exactly the mapping's values."""
        pristine = self.pristine
        if pristine is None:
            return True
        size = len(root) - sum(1 for key in _STACK_KEYS if key in root)
        if size != self.size:
            return False
        for key, value in root.items():
            if key in _STACK_KEYS:
                continue
            item = dict.get(pristine, key, _MISSING)
            if type(item) is not _LazyValue or item.value is not value:
                return False
        return True


_MISSING = object()


class _Registry:
    """Sessions of one outermost template evaluation."""

    __slots__ = ("__weakref__", "children", "roots")

    def __init__(self) -> None:
        self.roots: dict[tuple[int, int], Session] = {}
        self.children: weakref.WeakValueDictionary[int, Session] = (
            weakref.WeakValueDictionary()
        )


class Session:
    """One variable mapping during one template evaluation.

    root      the mapping, as lookup plugins receive it
    state     prepared view and reusable renders for the mapping's contents
    templar   templar whose renders go through the session
    """

    __slots__ = (
        "__weakref__",
        "engine",
        "loader",
        "marker_behavior",
        "registry",
        "root",
        "state",
    )

    def __init__(
        self,
        root: dict[str, Any],
        state: _State,
        marker_behavior: Any,
        loader: Any,
        registry: _Registry,
    ) -> None:
        self.root = root
        self.state = state
        self.marker_behavior = marker_behavior
        self.loader = loader
        self.registry = weakref.ref(registry)
        engine = _SessionEngine(loader=loader, marker_behavior=marker_behavior)
        engine._saltbox_session = self
        self.engine = engine

    @property
    def templar(self) -> Any:
        """A new templar rendering through the session (with its own Jinja environment)."""
        return Templar._from_template_engine(self.engine)

    def _check(self) -> None:
        state = self.state
        if state.epoch != _EPOCH:
            if not state.matches(self.root):
                state.reset()
            state.epoch = _EPOCH

    def _build(self, engine: Any) -> None:
        """Prepare the lazy view of the mapping, without the recursion-guard keys."""
        with TemplateContext(
            template_value=None, templar=engine, options=TemplateOptions.DEFAULT
        ):
            pristine = _AnsibleLazyTemplateMixin._try_create(self.root)
        if type(pristine) is not _AnsibleLazyTemplateDict:
            raise TypeError(f"unexpected lazy view type {type(pristine).__name__}")
        for key in _STACK_KEYS:
            if dict.__contains__(pristine, key):
                dict.__delitem__(pristine, key)
        self.state.pristine = pristine
        self.state.size = len(pristine)

    def fresh_view(self, engine: Any) -> Any:
        """Return a lazy view of the mapping as it is now, for one render by ``engine``."""
        self._check()
        state = self.state
        if state.pristine is None:
            try:
                self._build(engine)
            except Exception as error:  # noqa: BLE001 - internals changed, use plain path
                _switch_off(f"internal error while building a session ({error})")
                return self.root
        view = _AnsibleLazyTemplateDict(state.pristine)
        view._templar = engine
        root = self.root
        for key in _STACK_KEYS:
            if key in root:
                dict.__setitem__(view, key, _LazyValue(root[key]))
        return view

    def _bases(self) -> tuple[Any, ...]:
        root = self.root
        bases: list[Any] = []
        for key in _STACK_KEYS:
            stack = root.get(key)
            if type(stack) is list:
                bases.append((stack, len(stack)))
            else:
                bases.append((None, 0))
        return tuple(bases)

    def note_guard(self, stack_key: str, guard: str, present: bool) -> None:
        """Record a recursion-guard check made by role_var or docker_vars."""
        stack = self.root.get(stack_key)
        index = _STACK_KEYS.index(stack_key)
        frames = _frames()
        if present and frames:
            # A cycle: the error names every guard on the stack, so a render that went on after
            # it (errors='ignore', a default) depends on the whole stack and is not reused.
            frames[-1].pure = False
        for frame in frames:
            base, length = frame.bases[index]
            if type(stack) is list:
                pushed = stack[length:] if base is stack else stack
                if guard in pushed:
                    continue
            frame.guards.append((stack_key, guard, present))

    def _guards_hold(self, guards: tuple[Any, ...]) -> bool:
        root = self.root
        for stack_key, guard, present in guards:
            stack = root.get(stack_key)
            if ((type(stack) is list) and guard in stack) != present:
                return False
        return True

    def render(
        self, engine: Any, variable: Any, options: Any, mode: Any, lazy_options: Any
    ) -> Any:
        """Render a string through the session, reusing an earlier identical render if pure."""
        ctx = TemplateContext.current(optional=True)
        if ctx is None:
            return TemplateEngine.template(
                engine, variable, options=options, mode=mode, lazy_options=lazy_options
            )
        self._check()
        root = self.root
        cache = self.state.cache
        try:
            key: Any = (
                id(variable),
                options,
                mode,
                lazy_options,
                ctx.stop_on_template,
                id(engine._marker_behavior),
                _STACK_KEYS[0] in root,
                _STACK_KEYS[1] in root,
            )
            entry = cache.get(key)
        except TypeError:
            key = None
            entry = None
        if (
            entry is not None
            and entry[0] is variable
            and self._guards_hold(entry[3])
            and not _compares_identity(ctx)
        ):
            for name, args, kwargs in entry[2]:
                getattr(display, name)(*args, **kwargs)
            for stack_key, guard, present in entry[3]:
                self.note_guard(stack_key, guard, present)
            return _fresh(entry[1])

        frames = _frames()
        frame = _Frame(self._bases())
        frames.append(frame)
        try:
            result = TemplateEngine.template(
                engine, variable, options=options, mode=mode, lazy_options=lazy_options
            )
        finally:
            frames.pop()
            if frames:
                parent = frames[-1]
                parent.log.extend(frame.log)
                if not frame.pure:
                    parent.pure = False
        if key is not None and frame.pure and frame.compiled and _cacheable(result):
            cache[key] = (
                variable,
                _fresh(result),
                tuple(frame.log),
                tuple(frame.guards),
            )
        return result

    def child(self, root: dict[str, Any], name: str, instance: Any) -> Session:
        """Return the session for ``root``, this mapping with ``name`` set to ``instance``."""
        registry = self.registry()
        if registry is None:
            raise AnsibleLookupError(
                "[saltbox_var] Session used after its evaluation ended"
            )
        self._check()
        parent = self.state
        try:
            key: Any = (name, instance, AnsibleTagHelper.tags(instance))
            state = parent.children.get(key)
        except TypeError:
            key = None
            state = None
        if state is None:
            state = _State()
            if parent.pristine is not None:
                pristine = _AnsibleLazyTemplateDict(parent.pristine)
                dict.__setitem__(pristine, name, _LazyValue(instance))
                state.pristine = pristine
                state.size = len(pristine)
            if key is not None:
                parent.children[key] = state
        session = Session(
            root,
            state,
            self.marker_behavior,
            self.loader,
            registry,
        )
        registry.children[id(root)] = session
        return session


# The sessions of an evaluation are kept in this attribute of its top-level TemplateContext and
# nowhere else, so they go away with the evaluation.
_REGISTRY_ATTRIBUTE = "_saltbox_sessions"


def session_for(variables: Any, templar: Any) -> Session | None:
    """Return the session for a lookup's variables, or None for the plain path."""
    if not _session_enabled():
        return None
    ctx = TemplateContext.current(optional=True)
    if ctx is None:
        return None
    top = ctx
    while (parent := top._parent_ctx) is not None:
        top = parent
    registry = top.__dict__.get(_REGISTRY_ATTRIBUTE)
    if registry is None:
        registry = _Registry()
        setattr(top, _REGISTRY_ATTRIBUTE, registry)

    marker_behavior = templar._engine.marker_behavior
    key = (id(variables), id(marker_behavior))
    session = registry.roots.get(key)
    if session is not None and session.root is variables:
        return session
    session = registry.children.get(id(variables))
    if (
        session is not None
        and session.root is variables
        and session.marker_behavior is marker_behavior
    ):
        return session

    variables_type = type(variables)
    if variables_type is not dict and variables_type is not _AnsibleTaggedDict:
        return None
    if _try_get_internal_tags_mapping(variables):
        return None
    try:
        session = Session(
            variables,
            _State(),
            marker_behavior,
            templar._engine._loader,
            registry,
        )
    except Exception as error:  # noqa: BLE001 - internals changed, use plain path
        _switch_off(f"internal error while creating a session ({error})")
        return None
    registry.roots[key] = session
    return session


class LookupModule(LookupBase):
    session_for = staticmethod(session_for)

    def run(  # type: ignore[override]
        self, terms: list[Any], variables: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Any]:
        """Return each named variable as a direct reference in the calling template would."""
        if "session" in kwargs:
            raise AnsibleLookupError(
                "[saltbox_var] 'session' is not a lookup argument; set SALTBOX_LOOKUP_SESSION "
                "or 'session' in the [saltbox_lookups] section of ansible.cfg"
            )
        if variables is None:
            variables = {}
        self.set_options(var_options=variables, direct=kwargs)
        if self._templar is None:
            raise AnsibleLookupError("[saltbox_var] Templar is not initialized")

        templar = self._templar
        session = session_for(variables, templar)
        if session is not None:
            templar = session.templar
        default = self.get_option("default")
        values: list[Any] = []
        for term in terms:
            if not isinstance(term, str):
                raise AnsibleLookupError(
                    f"[saltbox_var] Variable name must be a string, not {type(term).__name__}"
                )
            if term in variables:
                values.append(variables[term])
            elif default is not None:
                values.append(default)
            else:
                values.append(UndefinedMarker(name=term))
        return templar._engine.template(values)
