# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
    name: role_var
    description:
      - This lookup replicates lookup('vars', traefik_role_var + suffix, default=lookup('vars', role_name + '_role' + suffix))
      - For the '_name' suffix, the fallback uses role_name + '_name' instead of role_name + '_role_name'
      - When 'role' parameter is specified, constructs the appropriate traefik_role_var for that role
      - For _name variables with dashes, checks both original and underscore-converted versions
      - Automatically converts lists of JSON strings to dictionaries when detected
    author: salty
    options:
      _terms:
        description: The suffix to append (e.g. '_dns_record')
        required: true
      default:
        description: The default value to return if neither variable is found
        type: raw
        required: false
      role:
        description: The role name to use for lookup instead of the current role_name
        type: str
        required: false
      convert_json:
        description: Whether to automatically convert JSON string lists to dictionaries (default true)
        type: bool
        required: false
        default: true
"""

EXAMPLES = """
- name: Look up a role variable with fallback
  vars:
    role_name: "traefik"
    traefik_role_var: "traefik"
  debug:
    msg: "{{ lookup('role_var', '_name', default=role_name) }}"
"""

from ansible.plugins.lookup import LookupBase
from ansible.errors import AnsibleLookupError, AnsibleUndefinedVariable
from ansible.utils.display import Display
from typing import Any, List, Optional, Dict
import json

from jinja2 import Undefined

display = Display()

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
                    display.warning(f"[role_var] JSON string parsed to non-dict: {parsed}")
                    return None
            except json.JSONDecodeError as je:
                display.warning(f"[role_var] Invalid JSON in: {json_str[:100]}... Error: {je}")
                return None
            except (TypeError, AttributeError) as e:
                display.warning(f"[role_var] Failed to process JSON string: {e}")
                return None

        display.vvv(f"[role_var] Converted JSON list to dict with {len(combined_dict)} keys using manual parsing")
        return combined_dict

    def _check_for_undefined(self, value: Any, var_name: str) -> None:
        """Recursively check for undefined variables in a result and raise a clear error if found"""
        if isinstance(value, Undefined):
            undefined_name = getattr(value, '_undefined_name', str(value))
            raise AnsibleUndefinedVariable(
                f"[role_var] Variable '{var_name}' contains undefined variable: {undefined_name}"
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
            msg = f"[role_var] Variable '{var_name}' references an undefined variable"
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

    def run(self, terms: List[str], variables: Optional[Dict[str, Any]] = None, **kwargs: Any) -> List[Any]:  # type: ignore[override]
        if variables is None:
            variables = {}

        suffix: str = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default: Any = self.get_option('default')
        specified_role: Optional[str] = self.get_option('role')
        convert_json: Optional[bool] = self.get_option('convert_json')
        if convert_json is None:
            convert_json = True

        if self._templar is None:
            raise AnsibleLookupError("[role_var] Templar is not initialized")
        self._templar.available_variables = variables

        # Use specified role if provided, otherwise fall back to role_name
        if specified_role:
            role_name: str = self._templar.template(specified_role, fail_on_undefined=True)
        else:
            if 'role_name' not in variables:
                raise KeyError("[role_var] Required variable 'role_name' not found")
            role_name: str = self._templar.template(variables['role_name'], fail_on_undefined=True)
        
        # If a custom role is specified, we need to construct the appropriate traefik_role_var for that role
        traefik_role_var: str
        if specified_role:
            # Replicate the logic: traefik_role_var: "{{ lookup('vars', role_name + '_name', default=role_name) }}"
            custom_role_name_var: str = role_name + '_name'
            if custom_role_name_var in variables:
                traefik_role_var = self._templar.template(variables[custom_role_name_var], fail_on_undefined=True)
            else:
                traefik_role_var = role_name
            display.vvv(f"[role_var] Using custom traefik_role_var for role '{role_name}': {traefik_role_var}")
        else:
            if 'traefik_role_var' not in variables:
                raise KeyError("[role_var] Required variable 'traefik_role_var' not found")
            traefik_role_var = self._templar.template(variables['traefik_role_var'], fail_on_undefined=True)

        # Build the variable names to check
        primary_var: str
        fallback_var: str
        if suffix == '_name':
            primary_var = traefik_role_var + suffix
            fallback_var = role_name + suffix
        else:
            primary_var = traefik_role_var + suffix
            fallback_var = role_name + '_role' + suffix

        # Create list of all variable names to check (including dash/underscore variants)
        vars_to_check: List[str] = []
        
        for var_name in [primary_var, fallback_var]:
            vars_to_check.append(var_name)
            # If the variable name contains dashes, also check the underscore version
            if '-' in var_name:
                underscore_var = var_name.replace('-', '_')
                vars_to_check.append(underscore_var)
                display.vvv(f"[role_var] Added underscore variant: {underscore_var} for {var_name}")

        display.vvv(f"[role_var] Checking these keys in order: {vars_to_check}")
        if display.verbosity >= 3:
            debug_keys = sorted([
                k for k in variables
                if suffix in k or k.endswith(suffix) or k.startswith((traefik_role_var, role_name))
            ])
            display.vvv(f"[role_var] Relevant vars: {debug_keys}")

        # Try each variable name in order
        for var_name in vars_to_check:
            if var_name in variables:
                raw_value = variables.get(var_name)
                if raw_value is None:
                    display.vvv(f"[role_var] Skipping {var_name} (value is None)")
                    continue

                # Variable exists - if templating fails, that's an error (don't fall back to default)
                # This ensures that variables referencing undefined vars are caught
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=True)
                except Exception as e:
                    raise AnsibleLookupError(
                        f"[role_var] Failed to resolve '{var_name}': {e}"
                    ) from e
                # Check for undefined variables that got captured instead of raising
                self._check_for_undefined(result, var_name)
                if result is not None:
                    # Check if we should convert JSON list to dict
                    if convert_json and self._is_json_string_list(result):
                        display.vvv(f"[role_var] Found JSON string list for {var_name}, converting to dict")
                        converted = self._convert_json_list_to_dict(result)
                        if converted is not None:
                            return [converted]
                        else:
                            display.vvv(f"[role_var] Conversion failed, returning original list")

                    display.vvv(f"[role_var] Returning templated value for {var_name}: {result}")
                    return [result]
                else:
                    display.vvv(f"[role_var] {var_name} is None after templating, skipping")
            else:
                display.vvv(f"[role_var] {var_name} not found in variables — skipping")

        # If we have a default, use it (only reached if no variable existed)
        if default is not None:
            display.vvv(f"[role_var] No usable variable found, returning default: {default}")
            return [default]

        # Otherwise raise an error - variable not found and no default provided
        raise AnsibleLookupError(
            f"[role_var] Variable not found and no default provided. "
            f"Tried the following variables in order: {', '.join(vars_to_check)}"
        )
