import os
import hashlib
import json

from ansible.plugins.lookup import LookupBase
from ansible.errors import AnsibleError
from ansible.utils.display import Display
from ansible.module_utils.six import string_types

display = Display()

DOCUMENTATION = '''
    name: role_var
    author: You
    version_added: "N/A"
    short_description: Look up a role variable with automatic fallback and caching
    description:
      - This lookup replicates: lookup('vars', role_name + suffix, default=lookup('vars', traefik_role_var + '_role' + suffix))
      - Caches only the returned variable (primary or fallback) per suffix
      - Avoids redundant hashing within a single playbook run
      - Skips cache for variables passed via --extra-vars
    options:
      _terms:
        description: The suffix to append (e.g. '_dns_record')
        required: True
      default:
        description: The default value to return if neither variable is found
        type: raw
        required: False
'''

OMIT_PLACEHOLDER = "__OMIT__"
_file_hash_cache = {}

class LookupModule(LookupBase):

    def run(self, terms, variables=None, **kwargs):
        if variables is None:
            variables = {}

        suffix = terms[0] if terms else ''
        self.set_options(var_options=variables, direct=kwargs)
        default = self.get_option('default')

        self._templar.available_variables = variables
        omit = variables.get('omit')

        playbook_dir = variables.get('playbook_dir') or os.getcwd()
        foldername = os.path.basename(playbook_dir.rstrip('/'))
        cache_path = f'/srv/git/saltbox/cache-{foldername}.json'

        role_name = variables.get('role_name')
        if isinstance(role_name, string_types):
            try:
                role_name = self._templar.template(role_name, fail_on_undefined=False)
            except Exception:
                role_name = None
        else:
            role_name = None

        traefik_role_var = None
        if role_name:
            name_var = role_name + '_name'
            raw_value = variables.get(name_var)
            if raw_value is not None:
                try:
                    traefik_role_var = self._templar.template(raw_value, fail_on_undefined=False)
                except Exception:
                    traefik_role_var = None
            else:
                traefik_role_var = role_name

        # Handle _name as a special case
        if suffix == '_name':
            primary_var = (role_name or '') + suffix
            fallback_var = (traefik_role_var or '') + suffix
        else:
            primary_var = (traefik_role_var or '') + suffix
            fallback_var = (role_name or '') + '_role' + suffix

        display.vvv(f"[role_var] Checking these keys: primary={primary_var}, fallback={fallback_var}")
        debug_keys = sorted([
            k for k in variables
            if suffix in k or k.endswith(suffix) or k.startswith((traefik_role_var or '', role_name or ''))
        ])
        display.vvv(f"[role_var] Relevant vars: {debug_keys}")

        watched_files = [
            '/srv/git/saltbox/accounts.yml',
            '/srv/git/saltbox/settings.yml',
            '/srv/git/saltbox/adv_settings.yml',
            '/srv/git/saltbox/motd.yml',
            '/srv/git/saltbox/backup_config.yml',
            '/srv/git/saltbox/hetzner_vlan.yml',
            '/srv/git/saltbox/providers.yml',
            '/srv/git/saltbox/inventories/group_vars/all.yml',
            '/srv/git/saltbox/inventories/host_vars/localhost.yml',
        ]
        if role_name:
            role_base = os.path.join(playbook_dir, f'roles/{role_name}')
            watched_files.append(os.path.join(role_base, 'defaults/main.yml'))
            watched_files.extend(self._find_task_files(os.path.join(role_base, 'tasks')))

        extra_var_keys = variables.get('__extra_var_keys__', [])
        skip_cache = primary_var in extra_var_keys or fallback_var in extra_var_keys

        if not skip_cache:
            for var_name in [primary_var, fallback_var]:
                result = self._get_cached_result(cache_path, watched_files, var_name, omit)
                if result is not None:
                    display.vvv(f"[role_var] Returning cached value for {var_name}: {result}")
                    return [result]

        for var_name in [primary_var, fallback_var]:
            if var_name in variables:
                raw_value = variables[var_name]
                if raw_value is None:
                    display.vvv(f"[role_var] Skipping {var_name} (value is None)")
                    continue
                try:
                    result = self._templar.template(raw_value, fail_on_undefined=False)
                    if result is not None and not (isinstance(result, str) and "{{" in result):
                        display.vvv(f"[role_var] Returning templated value for {var_name}: {result}")
                        if not skip_cache:
                            self._save_cache_result(cache_path, watched_files, var_name, result, omit)
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

    def _get_file_hash(self, path):
        if path in _file_hash_cache:
            return _file_hash_cache[path]
        try:
            with open(path, 'rb') as f:
                file_hash = hashlib.sha256(f.read()).hexdigest()
                _file_hash_cache[path] = file_hash
                return file_hash
        except (OSError, IOError):
            _file_hash_cache[path] = None
            return None

    def _get_cached_result(self, cache_path, file_paths, key, omit):
        if not os.path.exists(cache_path):
            return None

        try:
            with open(cache_path, 'r') as f:
                cache = json.load(f)
        except Exception:
            return None

        file_hashes = {path: self._get_file_hash(path) for path in file_paths}
        cached_entry = cache.get(key)
        if cached_entry and cached_entry.get('hash') == file_hashes:
            value = cached_entry.get('value')
            return omit if value == OMIT_PLACEHOLDER else value
        return None

    def _save_cache_result(self, cache_path, file_paths, key, value, omit):
        if value is None:
            display.vvv(f"[role_var] Not caching {key} (value is None)")
            return

        file_hashes = {path: self._get_file_hash(path) for path in file_paths}
        if value == omit:
            value = OMIT_PLACEHOLDER

        try:
            if os.path.exists(cache_path):
                with open(cache_path, 'r') as f:
                    cache = json.load(f)
            else:
                cache = {}

            cache[key] = {'hash': file_hashes, 'value': value}
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, 'w') as f:
                json.dump(cache, f, indent=2)
            display.vvv(f"[role_var] Cached value for {key}: {value}")
        except Exception as e:
            raise AnsibleError(f"Failed to write cache to {cache_path}: {e}")

    def _find_task_files(self, tasks_dir):
        found = []
        for root, _, files in os.walk(tasks_dir):
            for file in files:
                if file.endswith(('.yml', '.yaml')):
                    found.append(os.path.join(root, file))
        return found
