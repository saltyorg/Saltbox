# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
    name: docker_vars
    description:
      - Bulk lookup for docker variables using the same resolution logic as C(docker_var), but resolves multiple suffixes in one call.
      - For the '_name' suffix, the fallback uses _var_prefix + '_name' instead of _var_prefix + '_role_name'.
      - For instance names or var prefixes with dashes, checks both original and underscore-converted versions.
      - Automatically converts lists of JSON strings to dictionaries when detected.
    author: salty
    options:
      _terms:
        description: The suffixes to append (e.g. '_docker_network_mode')
        required: true
      specs:
        description: >-
          Mapping of suffix to spec dict (keys: default, omit, required). When provided,
          _terms/defaults are ignored.
        type: dict
        required: false
      defaults:
        description: Mapping of suffix to default value if neither variable is found
        type: dict
        required: false
      convert_json:
        description: Whether to automatically convert JSON string lists to dictionaries (default true)
        type: bool
        required: false
        default: true
"""

EXAMPLES = """
- name: Look up multiple docker variables with fallback in a single call
  vars:
    _docker_var_suffixes:
      - "_docker_container"
      - "_docker_network_mode"
    _docker_var_defaults:
      _docker_container: "myapp"
      _docker_network_mode: "bridge"
  debug:
    msg: "{{ lookup('docker_vars', _docker_var_suffixes, defaults=_docker_var_defaults) }}"

- name: Look up multiple docker variables with specs
  vars:
    _docker_var_specs:
      _docker_container:
        default: "myapp"
      _docker_network_mode:
        default: "bridge"
      _docker_auto_remove:
        omit: true
      _docker_image:
        required: true
  debug:
    msg: "{{ lookup('docker_vars', specs=_docker_var_specs) }}"
"""

from ansible.plugins.lookup import LookupBase
from ansible.errors import AnsibleLookupError, AnsibleUndefinedVariable, AnsibleValueOmittedError
from ansible.utils.display import Display
from typing import Any, List, Optional, Dict
import json

from jinja2 import Undefined

display = Display()
_OMIT_SENTINEL = object()

class LookupModule(LookupBase):

    def _is_json_string_list(self, value: Any) -> bool:
        """Check if value is a list of JSON strings"""
        if not isinstance(value, list):
            return False

        # Check if all items are strings that look like JSON objects
        for item in value:
            if not isinstance(item, str):
                return False
            stripped = item.strip()
            if not (stripped.startswith('{') and stripped.endswith('}')):
                return False

        return len(value) > 0

    def _convert_json_list_to_dict(self, json_list: List[str]) -> Optional[Dict[str, Any]]:
        """Convert a list of JSON strings to a combined dictionary"""
        combined_dict: Dict[str, Any] = {}

        for json_str in json_list:
            try:
                # Parse the JSON string directly
                parsed = json.loads(json_str)
                if isinstance(parsed, dict):
                    combined_dict.update(parsed)
                else:
                    display.warning(f"[docker_vars] JSON string parsed to non-dict: {parsed}")
                    return None
            except json.JSONDecodeError as je:
                display.warning(f"[docker_vars] Invalid JSON in: {json_str[:100]}... Error: {je}")
                return None
            except (TypeError, AttributeError) as e:
                display.warning(f"[docker_vars] Failed to process JSON string: {e}")
                return None

        display.vvv(f"[docker_vars] Converted JSON list to dict with {len(combined_dict)} keys using manual parsing")
        return combined_dict

    def _check_for_undefined(self, value: Any, var_name: str) -> None:
        """Recursively check for undefined variables in a result and raise a clear error if found"""
        if isinstance(value, Undefined):
            undefined_name = getattr(value, '_undefined_name', str(value))
            raise AnsibleUndefinedVariable(
                f"[docker_vars] Variable '{var_name}' contains undefined variable: {undefined_name}"
            )
        # Check for CapturedExceptionMarker or similar sentinel types
        type_name = type(value).__name__
        if 'Captured' in type_name or 'Undefined' in type_name or 'Marker' in type_name:
            # Try to extract more info from the marker
            undefined_name = getattr(value, '_undefined_name', None)
            if undefined_name is None:
                undefined_name = getattr(value, 'name', None)
            if undefined_name is None:
                # Try to get it from string representation
                value_str = str(value)
                if value_str and value_str != type_name:
                    undefined_name = value_str
            msg = f"[docker_vars] Variable '{var_name}' references an undefined variable"
            if undefined_name:
                msg += f": '{undefined_name}'"
            else:
                msg += f" (found {type_name})"
            raise AnsibleUndefinedVariable(msg)
        if isinstance(value, list):
            for i, item in enumerate(value):
                self._check_for_undefined(item, f"{var_name}[{i}]")
        elif isinstance(value, dict):
            for k, v in value.items():
                self._check_for_undefined(v, f"{var_name}.{k}")

    def _normalize_terms(self, terms: List[Any]) -> List[Any]:
        if len(terms) == 1 and isinstance(terms[0], (list, tuple)):
            return list(terms[0])
        return list(terms)

    def _build_vars_to_check(
        self,
        suffix: str,
        instance_names_to_try: List[str],
        var_prefixes_to_try: List[str],
    ) -> List[str]:
        vars_to_check: List[str] = []

        for inst_name in instance_names_to_try:
            vars_to_check.append(inst_name + suffix)

        for var_pref in var_prefixes_to_try:
            if suffix == '_name':
                fallback_var = var_pref + suffix
            else:
                fallback_var = var_pref + '_role' + suffix
            vars_to_check.append(fallback_var)

        return vars_to_check

    def _resolve_suffix(
        self,
        suffix: str,
        variables: Dict[str, Any],
        default_set: bool,
        default: Any,
        convert_json: bool,
        omit_token: Any,
        stack: List[str],
        var_prefix: str,
        instance_name: str,
        instance_names_to_try: List[str],
        var_prefixes_to_try: List[str],
    ) -> Any:
        if self._templar is None:
            raise AnsibleLookupError("[docker_vars] Templar is not initialized")
        templar = self._templar

        vars_to_check = self._build_vars_to_check(suffix, instance_names_to_try, var_prefixes_to_try)

        display.vvv(f"[docker_vars] Checking these keys in order for suffix '{suffix}': {vars_to_check}")
        if display.verbosity >= 3:
            debug_keys = sorted([
                k for k in variables
                if suffix in k or k.endswith(suffix) or any(k.startswith(prefix) for prefix in instance_names_to_try + var_prefixes_to_try)
            ])
            display.vvv(f"[docker_vars] Relevant vars for suffix '{suffix}': {debug_keys}")

        for var_name in vars_to_check:
            if var_name in variables:
                raw_value = variables.get(var_name)
                if raw_value is None:
                    display.vvv(f"[docker_vars] Skipping {var_name} (value is None)")
                    continue
                if omit_token is not None and raw_value is omit_token:
                    display.vvv(f"[docker_vars] {var_name} is omit — returning omit")
                    return omit_token

                guard_id = f"docker_var:{var_prefix}:{instance_name}:{suffix}:{var_name}"
                if guard_id in stack:
                    cycle = " -> ".join(stack + [guard_id])
                    raise AnsibleLookupError(
                        f"[docker_vars] Circular reference detected while resolving '{var_name}' "
                        f"(prefix='{var_prefix}', instance='{instance_name}', suffix='{suffix}'). Stack: {cycle}"
                    )
                stack.append(guard_id)
                try:
                    try:
                        result = templar.template(raw_value, fail_on_undefined=True)
                    except AnsibleValueOmittedError:
                        display.vvv(f"[docker_vars] {var_name} templated to omit — returning omit")
                        return omit_token
                    except Exception as e:
                        raise AnsibleLookupError(
                            f"[docker_vars] Failed to resolve '{var_name}': {e}"
                        ) from e
                    if omit_token is not None and result is omit_token:
                        display.vvv(f"[docker_vars] {var_name} resolved to omit — returning omit")
                        return omit_token
                    self._check_for_undefined(result, var_name)
                    if result is not None:
                        if convert_json and self._is_json_string_list(result):
                            display.vvv(f"[docker_vars] Found JSON string list for {var_name}, converting to dict")
                            converted = self._convert_json_list_to_dict(result)
                            if converted is not None:
                                return converted
                            else:
                                display.vvv(f"[docker_vars] Conversion failed, returning original list")

                        display.vvv(f"[docker_vars] Returning templated value for {var_name}: {result}")
                        return result
                    else:
                        display.vvv(f"[docker_vars] {var_name} is None after templating, skipping")
                finally:
                    if stack and stack[-1] == guard_id:
                        stack.pop()
                    elif guard_id in stack:
                        stack.remove(guard_id)
            else:
                display.vvv(f"[docker_vars] {var_name} not found in variables — skipping")

        if default_set:
            display.vvv(f"[docker_vars] No usable variable found for suffix '{suffix}', returning default: {default}")
            return default

        raise AnsibleLookupError(
            f"[docker_vars] Variable not found and no default provided for suffix '{suffix}'. "
            f"Tried the following variables in order: {', '.join(vars_to_check)}"
        )

    def run(self, terms: List[Any], variables: Optional[Dict[str, Any]] = None, **kwargs: Any) -> List[Any]:  # type: ignore[override]
        if variables is None:
            variables = {}

        self.set_options(var_options=variables, direct=kwargs)
        specs: Optional[Dict[str, Any]] = self.get_option('specs')
        defaults: Optional[Dict[str, Any]] = self.get_option('defaults')
        convert_json: Optional[bool] = self.get_option('convert_json')
        if convert_json is None:
            convert_json = True
        omit_token = variables.get('omit')

        use_specs = specs is not None
        if use_specs:
            if not isinstance(specs, dict):
                raise AnsibleLookupError("[docker_vars] 'specs' must be a dict when provided")
            suffixes = list(specs.keys())
        else:
            suffixes = self._normalize_terms(terms)

        if defaults is None:
            defaults = {}
        if defaults is not None and not isinstance(defaults, dict):
            raise AnsibleLookupError("[docker_vars] 'defaults' must be a dict when provided")

        if self._templar is None:
            raise AnsibleLookupError("[docker_vars] Templar is not initialized")
        self._templar.available_variables = variables

        stack_key = "__saltbox_docker_var_stack__"
        stack_prev = variables.get(stack_key)
        stack_owner = False
        if not isinstance(stack_prev, list):
            variables[stack_key] = []
            stack_owner = True
        stack = variables[stack_key]

        try:
            if '_var_prefix' not in variables:
                raise KeyError("[docker_vars] Required variable '_var_prefix' not found")
            if '_instance_name' not in variables:
                raise KeyError("[docker_vars] Required variable '_instance_name' not found")

            var_prefix: str = self._templar.template(variables['_var_prefix'], fail_on_undefined=True)
            instance_name: str = self._templar.template(variables['_instance_name'], fail_on_undefined=True)

            instance_names_to_try: List[str] = [instance_name]
            if '-' in instance_name:
                underscore_instance: str = instance_name.replace('-', '_')
                instance_names_to_try.append(underscore_instance)
                display.vvv(f"[docker_vars] Added underscore variant for instance: {underscore_instance}")

            var_prefixes_to_try: List[str] = [var_prefix]
            if '-' in var_prefix:
                underscore_prefix: str = var_prefix.replace('-', '_')
                var_prefixes_to_try.append(underscore_prefix)
                display.vvv(f"[docker_vars] Added underscore variant for prefix: {underscore_prefix}")

            results: Dict[str, Any] = {}
            for suffix in suffixes:
                if use_specs:
                    spec = specs.get(suffix, {}) if specs is not None else {}
                    if spec is None:
                        spec = {}
                    if not isinstance(spec, dict):
                        raise AnsibleLookupError(
                            f"[docker_vars] Spec for '{suffix}' must be a dict when provided"
                        )
                    spec_has_default = 'default' in spec
                    spec_default = spec.get('default')
                    spec_omit = bool(spec.get('omit', False))
                    spec_required = bool(spec.get('required', False))

                    default_set = spec_has_default or spec_omit
                    if spec_required:
                        default_set = False
                        spec_omit = False
                    if spec_has_default:
                        default_value = spec_default
                    elif spec_omit:
                        default_value = _OMIT_SENTINEL
                    else:
                        default_value = None

                    resolved = self._resolve_suffix(
                        suffix=suffix,
                        variables=variables,
                        default_set=default_set,
                        default=default_value,
                        convert_json=convert_json,
                        omit_token=omit_token,
                        stack=stack,
                        var_prefix=var_prefix,
                        instance_name=instance_name,
                        instance_names_to_try=instance_names_to_try,
                        var_prefixes_to_try=var_prefixes_to_try,
                    )

                    omit_value = False
                    if resolved is _OMIT_SENTINEL:
                        omit_value = True
                        resolved = None
                    elif omit_token is not None and resolved is omit_token:
                        omit_value = True
                        resolved = None
                    results[suffix] = {
                        "value": resolved,
                        "omit": omit_value,
                    }
                else:
                    default_set = bool(defaults) and suffix in defaults
                    default_value: Any = defaults[suffix] if default_set else None
                    results[suffix] = self._resolve_suffix(
                        suffix=suffix,
                        variables=variables,
                        default_set=default_set,
                        default=default_value,
                        convert_json=convert_json,
                        omit_token=omit_token,
                        stack=stack,
                        var_prefix=var_prefix,
                        instance_name=instance_name,
                        instance_names_to_try=instance_names_to_try,
                        var_prefixes_to_try=var_prefixes_to_try,
                    )

            return [results]
        finally:
            if stack_owner:
                if stack_prev is None:
                    variables.pop(stack_key, None)
                else:
                    variables[stack_key] = stack_prev
