from ansible.plugins.lookup import LookupBase
from ansible.errors import AnsibleLookupError, AnsibleUndefinedVariable
from ansible.utils.display import Display
from typing import Any, List, Optional, Dict
import json

# Try to import Jinja2's Undefined types to detect undefined variables in results
try:
    from jinja2 import Undefined
except ImportError:
    Undefined = type(None)  # Fallback if import fails

display = Display()

DOCUMENTATION = """
    name: docker_var
    author: salty
    version_added: "N/A"
    short_description: Look up a role variable with automatic fallback and JSON conversion
    description:
      - This lookup replicates lookup('vars', _instance_name + suffix, default=lookup('vars', _var_prefix + '_role' + suffix))
      - For instance names or var prefixes with dashes, checks both original and underscore-converted versions
      - Automatically converts lists of JSON strings to dictionaries when detected
    options:
      _terms:
        description: The suffix to append (e.g. '_docker_network_mode')
        required: true
      default:
        description: The default value to return if neither variable is found
        type: raw
        required: false
      convert_json:
        description: Whether to automatically convert JSON string lists to dictionaries (default true)
        type: bool
        required: false
        default: true
"""

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
                    display.warning(f"[docker_var] JSON string parsed to non-dict: {parsed}")
                    return None
            except json.JSONDecodeError as je:
                display.warning(f"[docker_var] Invalid JSON in: {json_str[:100]}... Error: {je}")
                return None
            except (TypeError, AttributeError) as e:
                display.warning(f"[docker_var] Failed to process JSON string: {e}")
                return None

        display.vvv(f"[docker_var] Converted JSON list to dict with {len(combined_dict)} keys using manual parsing")
        return combined_dict

    def _check_for_undefined(self, value: Any, var_name: str) -> None:
        """Recursively check for undefined variables in a result and raise a clear error if found"""
        if isinstance(value, Undefined):
            undefined_name = getattr(value, '_undefined_name', str(value))
            raise AnsibleUndefinedVariable(
                f"[docker_var] Variable '{var_name}' contains undefined variable: {undefined_name}"
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
            msg = f"[docker_var] Variable '{var_name}' references an undefined variable"
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

    def run(self, terms: List[str], variables: Optional[Dict[str, Any]] = None, **kwargs: Any) -> List[Any]:
        if variables is None:
            variables = {}
        
        suffix: str = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default: Any = self.get_option('default')
        convert_json: Optional[bool] = self.get_option('convert_json')
        if convert_json is None:
            convert_json = True

        self._templar.available_variables = variables

        if '_var_prefix' not in variables:
            raise KeyError("[docker_var] Required variable '_var_prefix' not found")
        if '_instance_name' not in variables:
            raise KeyError("[docker_var] Required variable '_instance_name' not found")

        var_prefix: str = self._templar.template(variables['_var_prefix'], fail_on_undefined=True)
        instance_name: str = self._templar.template(variables['_instance_name'], fail_on_undefined=True)
        
        # Create lists of prefixes to try (including dash/underscore variants)
        instance_names_to_try: List[str] = [instance_name]
        if '-' in instance_name:
            underscore_instance: str = instance_name.replace('-', '_')
            instance_names_to_try.append(underscore_instance)
            display.vvv(f"[docker_var] Added underscore variant for instance: {underscore_instance}")

        var_prefixes_to_try: List[str] = [var_prefix]
        if '-' in var_prefix:
            underscore_prefix: str = var_prefix.replace('-', '_')
            var_prefixes_to_try.append(underscore_prefix)
            display.vvv(f"[docker_var] Added underscore variant for prefix: {underscore_prefix}")

        # Build the variable names to check
        vars_to_check: List[str] = []
        
        # For each instance name variant, add the primary variable
        for inst_name in instance_names_to_try:
            primary_var: str = inst_name + suffix
            vars_to_check.append(primary_var)

        # For each var prefix variant, add the fallback variable
        for var_pref in var_prefixes_to_try:
            fallback_var: str
            if suffix == '_name':
                fallback_var = var_pref + suffix
            else:
                fallback_var = var_pref + '_role' + suffix
            vars_to_check.append(fallback_var)

        display.vvv(f"[docker_var] Checking these keys in order: {vars_to_check}")
        if display.verbosity >= 3:
            debug_keys = sorted([
                k for k in variables
                if suffix in k or k.endswith(suffix) or any(k.startswith(prefix) for prefix in instance_names_to_try + var_prefixes_to_try)
            ])
            display.vvv(f"[docker_var] Relevant vars: {debug_keys}")
        
        # Try each variable name in order
        for var_name in vars_to_check:
            if var_name in variables:
                raw_value = variables.get(var_name)
                if raw_value is None:
                    display.vvv(f"[docker_var] Skipping {var_name} (value is None)")
                    continue

                # Check if this is a role fallback variable (contains '_role_')
                # Role fallback variables depend on the role's _name variable being defined.
                # If the _name variable doesn't exist, skip this fallback since the role's
                # context isn't properly established (e.g., calling remove_docker_container
                # with var_prefix='redis' but redis_name isn't set).
                if '_role_' in var_name:
                    role_prefix = var_name.split('_role_')[0]
                    role_name_var = role_prefix + '_name'
                    if role_name_var not in variables:
                        display.vvv(f"[docker_var] Skipping role fallback {var_name} because {role_name_var} is not defined")
                        continue

                # Variable exists - if templating fails, that's an error (don't fall back to default)
                # This ensures that variables referencing undefined vars are caught
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=True)
                except Exception as e:
                    raise AnsibleLookupError(
                        f"[docker_var] Failed to resolve '{var_name}': {e}"
                    ) from e
                # Check for undefined variables that got captured instead of raising
                self._check_for_undefined(result, var_name)
                if result is not None:
                    # Check if we should convert JSON list to dict
                    if convert_json and self._is_json_string_list(result):
                        display.vvv(f"[docker_var] Found JSON string list for {var_name}, converting to dict")
                        converted = self._convert_json_list_to_dict(result)
                        if converted is not None:
                            return [converted]
                        else:
                            display.vvv(f"[docker_var] Conversion failed, returning original list")

                    display.vvv(f"[docker_var] Returning templated value for {var_name}: {result}")
                    return [result]
                else:
                    display.vvv(f"[docker_var] {var_name} is None after templating, skipping")
            else:
                display.vvv(f"[docker_var] {var_name} not found in variables — skipping")

        # If we have a default, use it (only reached if no variable existed)
        if default is not None:
            display.vvv(f"[docker_var] No usable variable found, returning default: {default}")
            return [default]

        # Otherwise raise an error - variable not found and no default provided
        raise AnsibleLookupError(
            f"[docker_var] Variable not found and no default provided. "
            f"Tried the following variables in order: {', '.join(vars_to_check)}"
        )
