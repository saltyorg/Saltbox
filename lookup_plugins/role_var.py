from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display
import json

display = Display()

DOCUMENTATION = """
    name: role_var
    author: You
    version_added: "N/A"
    short_description: Look up a role variable with automatic fallback and JSON conversion
    description:
      - This lookup replicates lookup('vars', traefik_role_var + suffix, default=lookup('vars', role_name + '_role' + suffix))
      - When 'role' parameter is specified, constructs the appropriate traefik_role_var for that role
      - For _name variables with dashes, checks both original and underscore-converted versions
      - Automatically converts lists of JSON strings to dictionaries when detected
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

class LookupModule(LookupBase):

    def _is_json_string_list(self, value):
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

    def _convert_json_list_to_dict(self, json_list):
        """Convert a list of JSON strings to a combined dictionary"""
        try:
            # Simple approach: do the JSON parsing manually like the manual method does
            combined_dict = {}
            
            for json_str in json_list:
                # Use the same from_json parsing that Ansible uses
                try:
                    import json
                    # Parse the JSON string directly
                    parsed = json.loads(json_str)
                    if isinstance(parsed, dict):
                        combined_dict.update(parsed)
                    else:
                        display.warning(f"[role_var] JSON string parsed to non-dict: {parsed}")
                except json.JSONDecodeError as je:
                    display.warning(f"[role_var] Invalid JSON in: {json_str[:100]}... Error: {je}")
                    return None
            
            display.vvv(f"[role_var] Converted JSON list to dict with {len(combined_dict)} keys using manual parsing")
            return combined_dict
            
        except Exception as e:
            display.warning(f"[role_var] Failed to convert JSON list: {e}")
            return None

    def run(self, terms, variables=None, **kwargs):
        if variables is None:
            variables = {}

        suffix = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default = self.get_option('default')
        specified_role = self.get_option('role')
        convert_json = self.get_option('convert_json')
        if convert_json is None:
            convert_json = True

        self._templar.available_variables = variables
        omit = variables.get('omit')

        # Use specified role if provided, otherwise fall back to role_name
        role_name = self._templar.template(specified_role, fail_on_undefined=False) if specified_role else self._templar.template(variables['role_name'], fail_on_undefined=False)
        
        # If a custom role is specified, we need to construct the appropriate traefik_role_var for that role
        if specified_role:
            # Replicate the logic: traefik_role_var: "{{ lookup('vars', role_name + '_name', default=role_name) }}"
            custom_role_name_var = role_name + '_name'
            if custom_role_name_var in variables:
                traefik_role_var = self._templar.template(variables[custom_role_name_var], fail_on_undefined=False)
            else:
                traefik_role_var = role_name
            display.vvv(f"[role_var] Using custom traefik_role_var for role '{role_name}': {traefik_role_var}")
        else:
            traefik_role_var = self._templar.template(variables['traefik_role_var'], fail_on_undefined=False)

        # Build the variable names to check
        if suffix == '_name':
            primary_var = traefik_role_var + suffix
            fallback_var = role_name + suffix
        else:
            primary_var = traefik_role_var + suffix
            fallback_var = role_name + '_role' + suffix

        # Create list of all variable names to check (including dash/underscore variants)
        vars_to_check = []
        
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
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=False)
                    if result is not None and not (isinstance(result, str) and "{{" in result):
                        # Check if we should convert JSON list to dict
                        if convert_json and self._is_json_string_list(result):
                            display.vvv(f"[role_var] Found JSON string list for {var_name}, converting to dict")
                            converted = self._convert_json_list_to_dict(result)
                            if converted is not None:
                                display.vvv(f"[role_var] Returning converted dict for {var_name}: {len(converted)} keys")
                                return [converted]
                            else:
                                display.vvv(f"[role_var] Conversion failed, returning original list")
                        
                        display.vvv(f"[role_var] Returning templated value for {var_name}: {result}")
                        return [result]
                    else:
                        display.vvv(f"[role_var] {var_name} is unresolved or empty, skipping (value: {result})")
                except Exception as e:
                    display.vvv(f"[role_var] Error templating {var_name}: {e}")
            else:
                display.vvv(f"[role_var] {var_name} not found in variables — skipping")

        fallback_result = default if default is not None else omit
        display.vvv(f"[role_var] No usable variable found, returning default or omit: {fallback_result}")
        return [fallback_result]
