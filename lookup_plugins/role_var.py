from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display

display = Display()

DOCUMENTATION = '''
    name: role_var
    author: You
    version_added: "N/A"
    short_description: Look up a role variable with automatic fallback
    description:
      - This lookup replicates: lookup('vars', traefik_role_var + suffix, default=lookup('vars', role_name + '_role' + suffix))
      - When 'role' parameter is specified, constructs the appropriate traefik_role_var for that role
    options:
      _terms:
        description: The suffix to append (e.g. '_dns_record')
        required: True
      default:
        description: The default value to return if neither variable is found
        type: raw
        required: False
      role:
        description: The role name to use for lookup instead of the current role_name
        type: str
        required: False
'''

class LookupModule(LookupBase):

    def run(self, terms, variables=None, **kwargs):
        if variables is None:
            variables = {}

        suffix = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default = self.get_option('default')
        specified_role = self.get_option('role')

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

        if suffix == '_name':
            primary_var = traefik_role_var + suffix
            fallback_var = role_name + suffix
        else:
            primary_var = traefik_role_var + suffix
            fallback_var = role_name + '_role' + suffix

        display.vvv(f"[role_var] Checking these keys: primary={primary_var}, fallback={fallback_var}")
        debug_keys = sorted([
            k for k in variables
            if suffix in k or k.endswith(suffix) or k.startswith((traefik_role_var, role_name))
        ])
        display.vvv(f"[role_var] Relevant vars: {debug_keys}")

        for var_name in [primary_var, fallback_var]:
            if var_name in variables:
                raw_value = variables.get(var_name)
                if raw_value is None:
                    display.vvv(f"[plugin] Skipping {var_name} (value is None)")
                    continue
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=False)
                    if result is not None and not (isinstance(result, str) and "{{" in result):
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
