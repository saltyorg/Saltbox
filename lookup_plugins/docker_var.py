from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display

display = Display()

DOCUMENTATION = '''
    name: docker_var
    author: You
    version_added: "N/A"
    short_description: Look up a role variable with automatic fallback
    description:
      - This lookup replicates: lookup('vars', _instance_name + suffix, default=lookup('vars', _var_prefix + '_role' + suffix))
    options:
      _terms:
        description: The suffix to append (e.g. '_docker_network_mode')
        required: True
      default:
        description: The default value to return if neither variable is found
        type: raw
        required: False
'''

class LookupModule(LookupBase):

    def run(self, terms, variables=None, **kwargs):
        if variables is None:
            variables = {}

        suffix = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default = self.get_option('default')

        self._templar.available_variables = variables
        omit = variables.get('omit')

        var_prefix = self._templar.template(variables['_var_prefix'], fail_on_undefined=False)
        instance_name = self._templar.template(variables['_instance_name'], fail_on_undefined=False)

        if suffix == '_name':
            primary_var = instance_name + suffix
            fallback_var = var_prefix + suffix
        else:
            primary_var = instance_name + suffix
            fallback_var = var_prefix + '_role' + suffix

        display.vvv(f"[docker_var] Checking these keys: primary={primary_var}, fallback={fallback_var}")
        debug_keys = sorted([
            k for k in variables
            if suffix in k or k.endswith(suffix) or k.startswith((instance_name, var_prefix))
        ])
        display.vvv(f"[docker_var] Relevant vars: {debug_keys}")

        for var_name in [primary_var, fallback_var]:
            if var_name in variables:
                raw_value = variables.get(var_name)
                if raw_value is None:
                    display.vvv(f"[plugin] Skipping {var_name} (value is None)")
                    continue
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=False)
                    if result is not None and not (isinstance(result, str) and "{{" in result):
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
