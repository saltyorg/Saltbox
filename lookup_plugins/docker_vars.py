from __future__ import annotations

DOCUMENTATION = """
    name: docker_vars
    description:
      - Resolves Docker variables in bulk using instance-over-role precedence.
      - For the '_name' suffix, the fallback uses _var_prefix + '_name' instead of _var_prefix + '_role_name'.
      - For instance names or variable prefixes with dashes, checks both original and underscore-converted versions.
      - Automatically converts lists of JSON strings to dictionaries when detected.
      - Returns a sparse mapping; unresolved omit specs are absent.
    author: salty
    options:
      _terms:
        description: Positional terms are not supported; use C(specs).
        required: false
      specs:
        description:
          - Mapping of suffix to a policy mapping.
          - Each policy must contain exactly one of C(default), C(omit=true), or C(required=true).
          - C(null) values continue through instance and role precedence.
          - Explicit Ansible omit values are accepted only by C(omit=true) policies.
          - Explicit omit values raise an error for C(default) and C(required=true) policies.
        type: dict
        required: true
      convert_json:
        description: Whether to automatically convert JSON string lists to dictionaries.
        type: bool
        required: false
        default: true
"""

EXAMPLES = """
- name: Resolve Docker variables in bulk
  vars:
    _docker_var_specs:
      _docker_container:
        default: "myapp"
      _docker_auto_remove:
        omit: true
      _docker_image:
        required: true
  debug:
    msg: "{{ lookup('docker_vars', specs=_docker_var_specs) }}"
"""

import json
from typing import Any

from ansible.errors import (
    AnsibleLookupError,
    AnsibleUndefinedVariable,
    AnsibleValueOmittedError,
)
from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display
from jinja2 import Undefined

display = Display()
_ABSENT = object()
_SPEC_KEYS = frozenset({"default", "omit", "required"})


class LookupModule(LookupBase):
    @staticmethod
    def _resolve_explicit_omit(
        suffix: str,
        spec: dict[str, Any],
        var_name: str,
    ) -> Any:
        """Return absence only when the suffix explicitly permits omit."""
        if spec.get("omit") is True:
            return _ABSENT

        policy = "required" if spec.get("required") is True else "default"
        raise AnsibleLookupError(
            f"[docker_vars] Variable '{var_name}' resolved to omit, but "
            f"'{suffix}' uses a {policy} policy"
        )

    @staticmethod
    def _is_json_string_list(value: Any) -> bool:
        """Return whether value is a non-empty list of JSON object strings."""
        if not isinstance(value, list) or not value:
            return False

        for item in value:
            if not isinstance(item, str):
                return False
            stripped = item.strip()
            if not (stripped.startswith("{") and stripped.endswith("}")):
                return False

        return True

    @staticmethod
    def _convert_json_list_to_dict(
        json_list: list[str],
    ) -> dict[str, Any] | None:
        """Convert a list of JSON object strings to one dictionary."""
        combined_dict: dict[str, Any] = {}

        for json_string in json_list:
            try:
                parsed = json.loads(json_string)
            except json.JSONDecodeError as error:
                display.warning(
                    f"[docker_vars] Invalid JSON in: {json_string[:100]}... "
                    f"Error: {error}"
                )
                return None
            except (AttributeError, TypeError) as error:
                display.warning(f"[docker_vars] Failed to process JSON string: {error}")
                return None

            if not isinstance(parsed, dict):
                display.warning(
                    f"[docker_vars] JSON string parsed to non-dict: {parsed}"
                )
                return None
            combined_dict.update(parsed)

        display.vvv(
            f"[docker_vars] Converted JSON list to dict with {len(combined_dict)} keys"
        )
        return combined_dict

    def _check_for_undefined(self, value: Any, var_name: str) -> None:
        """Raise a clear error for undefined values nested in a result."""
        if isinstance(value, Undefined):
            undefined_name = getattr(value, "_undefined_name", str(value))
            raise AnsibleUndefinedVariable(
                f"[docker_vars] Variable '{var_name}' contains undefined "
                f"variable: {undefined_name}"
            )

        type_name = type(value).__name__
        if any(marker in type_name for marker in ("Captured", "Undefined", "Marker")):
            undefined_name = getattr(value, "_undefined_name", None)
            if undefined_name is None:
                undefined_name = getattr(value, "name", None)
            if undefined_name is None:
                value_string = str(value)
                if value_string and value_string != type_name:
                    undefined_name = value_string

            message = (
                f"[docker_vars] Variable '{var_name}' references an undefined variable"
            )
            if undefined_name:
                message += f": '{undefined_name}'"
            else:
                message += f" (found {type_name})"
            raise AnsibleUndefinedVariable(message)

        if isinstance(value, list):
            for index, item in enumerate(value):
                self._check_for_undefined(item, f"{var_name}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                self._check_for_undefined(item, f"{var_name}.{key}")

    @staticmethod
    def _validate_spec(suffix: str, spec: Any, omit_token: Any) -> dict[str, Any]:
        """Validate and return one Docker variable policy mapping."""
        if not isinstance(spec, dict):
            raise AnsibleLookupError(
                f"[docker_vars] Spec for '{suffix}' must be a dict"
            )

        unknown_keys = set(spec) - _SPEC_KEYS
        if unknown_keys:
            raise AnsibleLookupError(
                f"[docker_vars] Spec for '{suffix}' has unknown keys: "
                f"{', '.join(sorted(unknown_keys))}"
            )

        has_default = "default" in spec
        is_omit = spec.get("omit") is True
        is_required = spec.get("required") is True
        policy_count = int(has_default) + int(is_omit) + int(is_required)
        if policy_count != 1:
            raise AnsibleLookupError(
                f"[docker_vars] Spec for '{suffix}' must define exactly one of "
                "default, omit=true, or required=true"
            )
        if "omit" in spec and spec.get("omit") is not True:
            raise AnsibleLookupError(
                f"[docker_vars] Spec for '{suffix}' must use omit=true"
            )
        if "required" in spec and spec.get("required") is not True:
            raise AnsibleLookupError(
                f"[docker_vars] Spec for '{suffix}' must use required=true"
            )
        if has_default and omit_token is not None and spec["default"] is omit_token:
            raise AnsibleLookupError(
                f"[docker_vars] Spec for '{suffix}' must use omit=true instead "
                "of default=omit"
            )

        return spec

    @staticmethod
    def _build_vars_to_check(
        suffix: str,
        instance_names: list[str],
        variable_prefixes: list[str],
    ) -> list[str]:
        """Build variable names in instance-over-role precedence order."""
        vars_to_check = [instance_name + suffix for instance_name in instance_names]
        for variable_prefix in variable_prefixes:
            if suffix == "_name":
                vars_to_check.append(variable_prefix + suffix)
            else:
                vars_to_check.append(variable_prefix + "_role" + suffix)
        return vars_to_check

    def _resolve_suffix(
        self,
        suffix: str,
        spec: dict[str, Any],
        variables: dict[str, Any],
        convert_json: bool,
        omit_token: Any,
        stack: list[str],
        variable_prefix: str,
        instance_name: str,
        instance_names: list[str],
        variable_prefixes: list[str],
    ) -> Any:
        """Resolve one suffix to a value or the internal absent sentinel."""
        if self._templar is None:
            raise AnsibleLookupError("[docker_vars] Templar is not initialized")
        templar = self._templar
        vars_to_check = self._build_vars_to_check(
            suffix,
            instance_names,
            variable_prefixes,
        )

        display.vvv(
            f"[docker_vars] Checking these keys for suffix '{suffix}': {vars_to_check}"
        )
        for var_name in vars_to_check:
            if var_name not in variables:
                display.vvv(
                    f"[docker_vars] {var_name} not found in variables — skipping"
                )
                continue

            raw_value = variables.get(var_name)
            if raw_value is None:
                display.vvv(f"[docker_vars] Skipping {var_name} (value is None)")
                continue
            if omit_token is not None and raw_value is omit_token:
                return self._resolve_explicit_omit(suffix, spec, var_name)

            guard_id = (
                f"docker_vars:{variable_prefix}:{instance_name}:{suffix}:{var_name}"
            )
            if guard_id in stack:
                cycle = " -> ".join([*stack, guard_id])
                raise AnsibleLookupError(
                    f"[docker_vars] Circular reference detected while resolving "
                    f"'{var_name}' (prefix='{variable_prefix}', "
                    f"instance='{instance_name}', suffix='{suffix}'). Stack: {cycle}"
                )

            stack.append(guard_id)
            try:
                try:
                    result = templar.template(raw_value, fail_on_undefined=True)
                except AnsibleValueOmittedError:
                    return self._resolve_explicit_omit(suffix, spec, var_name)
                except Exception as error:
                    raise AnsibleLookupError(
                        f"[docker_vars] Failed to resolve '{var_name}': {error}"
                    ) from error

                if omit_token is not None and result is omit_token:
                    return self._resolve_explicit_omit(suffix, spec, var_name)

                self._check_for_undefined(result, var_name)
                if result is None:
                    continue
                if convert_json and self._is_json_string_list(result):
                    converted = self._convert_json_list_to_dict(result)
                    if converted is not None:
                        result = converted

                display.vvv(
                    f"[docker_vars] Returning templated value for {var_name}: {result}"
                )
                return result
            finally:
                if stack and stack[-1] == guard_id:
                    stack.pop()
                elif guard_id in stack:
                    stack.remove(guard_id)

        if "default" in spec:
            default = spec["default"]
            display.vvv(
                f"[docker_vars] No usable variable found for suffix '{suffix}', "
                f"returning default: {default}"
            )
            return default
        if spec.get("omit") is True:
            return _ABSENT

        raise AnsibleLookupError(
            f"[docker_vars] Variable not found for required suffix '{suffix}'. "
            f"Tried the following variables in order: {', '.join(vars_to_check)}"
        )

    def run(  # type: ignore[override]
        self,
        terms: list[Any],
        variables: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Resolve the requested Docker variable specs."""
        if variables is None:
            variables = {}
        if terms:
            raise AnsibleLookupError(
                "[docker_vars] Positional terms are not supported; use specs="
            )

        self.set_options(var_options=variables, direct=kwargs)
        specs: Any = self.get_option("specs")
        convert_json: bool | None = self.get_option("convert_json")
        if convert_json is None:
            convert_json = True
        omit_token = variables.get("omit")

        if not isinstance(specs, dict):
            raise AnsibleLookupError("[docker_vars] 'specs' must be a dict")
        validated_specs = {
            suffix: self._validate_spec(suffix, spec, omit_token)
            for suffix, spec in specs.items()
        }

        if self._templar is None:
            raise AnsibleLookupError("[docker_vars] Templar is not initialized")
        self._templar.available_variables = variables

        stack_key = "__saltbox_docker_vars_stack__"
        previous_stack = variables.get(stack_key)
        owns_stack = False
        if not isinstance(previous_stack, list):
            variables[stack_key] = []
            owns_stack = True
        stack = variables[stack_key]

        try:
            if "_var_prefix" not in variables:
                raise KeyError(
                    "[docker_vars] Required variable '_var_prefix' not found"
                )
            if "_instance_name" not in variables:
                raise KeyError(
                    "[docker_vars] Required variable '_instance_name' not found"
                )

            variable_prefix: str = self._templar.template(
                variables["_var_prefix"],
                fail_on_undefined=True,
            )
            instance_name: str = self._templar.template(
                variables["_instance_name"],
                fail_on_undefined=True,
            )

            instance_names = [instance_name]
            if "-" in instance_name:
                instance_names.append(instance_name.replace("-", "_"))

            variable_prefixes = [variable_prefix]
            if "-" in variable_prefix:
                variable_prefixes.append(variable_prefix.replace("-", "_"))

            results: dict[str, Any] = {}
            for suffix, spec in validated_specs.items():
                resolved = self._resolve_suffix(
                    suffix=suffix,
                    spec=spec,
                    variables=variables,
                    convert_json=convert_json,
                    omit_token=omit_token,
                    stack=stack,
                    variable_prefix=variable_prefix,
                    instance_name=instance_name,
                    instance_names=instance_names,
                    variable_prefixes=variable_prefixes,
                )
                if resolved is not _ABSENT:
                    results[suffix] = resolved

            return [results]
        finally:
            if owns_stack:
                if previous_stack is None:
                    variables.pop(stack_key, None)
                else:
                    variables[stack_key] = previous_stack
