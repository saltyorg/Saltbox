from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display
import json

display = Display()

DOCUMENTATION = """
    name: docker_var
    author: You
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
                    # Parse the JSON string directly
                    parsed = json.loads(json_str)
                    if isinstance(parsed, dict):
                        combined_dict.update(parsed)
                    else:
                        display.warning(f"[docker_var] JSON string parsed to non-dict: {parsed}")
                except json.JSONDecodeError as je:
                    display.warning(f"[docker_var] Invalid JSON in: {json_str[:100]}... Error: {je}")
                    return None
            
            display.vvv(f"[docker_var] Converted JSON list to dict with {len(combined_dict)} keys using manual parsing")
            return combined_dict
            
        except Exception as e:
            display.warning(f"[docker_var] Failed to convert JSON list: {e}")
            return None

    def run(self, terms, variables=None, **kwargs):
        if variables is None:
            variables = {}
        
        suffix = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default = self.get_option('default')
        convert_json = self.get_option('convert_json')
        if convert_json is None:
            convert_json = True

        self._templar.available_variables = variables
        omit = variables.get('omit')
        
        var_prefix = self._templar.template(variables['_var_prefix'], fail_on_undefined=False)
        instance_name = self._templar.template(variables['_instance_name'], fail_on_undefined=False)
        
        # Create lists of prefixes to try (including dash/underscore variants)
        instance_names_to_try = [instance_name]
        if '-' in instance_name:
            underscore_instance = instance_name.replace('-', '_')
            instance_names_to_try.append(underscore_instance)
            display.vvv(f"[docker_var] Added underscore variant for instance: {underscore_instance}")
        
        var_prefixes_to_try = [var_prefix]
        if '-' in var_prefix:
            underscore_prefix = var_prefix.replace('-', '_')
            var_prefixes_to_try.append(underscore_prefix)
            display.vvv(f"[docker_var] Added underscore variant for prefix: {underscore_prefix}")

        # Build the variable names to check
        vars_to_check = []
        
        # For each instance name variant, add the primary variable
        for inst_name in instance_names_to_try:
            if suffix == '_name':
                primary_var = inst_name + suffix
            else:
                primary_var = inst_name + suffix
            vars_to_check.append(primary_var)
        
        # For each var prefix variant, add the fallback variable
        for var_pref in var_prefixes_to_try:
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
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=False)
                    if result is not None and not (isinstance(result, str) and "{{" in result):
                        # Check if we should convert JSON list to dict
                        if convert_json and self._is_json_string_list(result):
                            display.vvv(f"[docker_var] Found JSON string list for {var_name}, converting to dict")
                            converted = self._convert_json_list_to_dict(result)
                            if converted is not None:
                                display.vvv(f"[docker_var] Returning converted dict for {var_name}: {len(converted)} keys")
                                return [converted]
                            else:
                                display.vvv(f"[docker_var] Conversion failed, returning original list")
                        
                        display.vvv(f"[docker_var] Returning templated value for {var_name}: {result}")
                        return [result]
                    else:
                        display.vvv(f"[docker_var] {var_name} is unresolved or empty, skipping (value: {result})")
                except Exception as e:
                    display.vvv(f"[docker_var] Error templating {var_name}: {e}")
            else:
                display.vvv(f"[docker_var] {var_name} not found in variables — skipping")
        
        fallback_result = default if default is not None else omit
        display.vvv(f"[docker_var] No usable variable found, returning default or omit: {fallback_result}")
        return [fallback_result]
