import os
import hashlib
import json

from ansible.plugins.lookup import LookupBase
from ansible.errors import AnsibleError, AnsibleUndefinedVariable
from ansible.module_utils.six import string_types

DOCUMENTATION = '''
    name: docker_var
    author: You
    version_added: "N/A"
    short_description: Look up a role variable with automatic fallback and caching
    description:
      - This lookup replicates: lookup('vars', _instance_name + suffix, default=lookup('vars', _var_prefix + '_role' + suffix))
      - Caches only the returned variable (primary or fallback) per suffix
      - Avoids redundant hashing within a single playbook run
      - Skips cache for variables passed via --extra-vars
    options:
      _terms:
        description: The suffix to append (e.g. '_docker_network_mode')
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
        myvars = getattr(self._templar, '_available_variables', {})
        omit = myvars.get('omit')

        playbook_dir = myvars.get('playbook_dir') or os.getcwd()
        foldername = os.path.basename(playbook_dir.rstrip('/'))
        cache_path = f'/srv/git/saltbox/cache-{foldername}.json'

        try:
            var_prefix = self._get_var('_var_prefix', myvars)
            instance_name = self._get_var('_instance_name', myvars)
        except AnsibleUndefinedVariable:
            var_prefix = instance_name = None

        primary_var = (instance_name or '') + suffix
        fallback_var = (var_prefix or '') + '_role' + suffix

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
        if var_prefix:
            role_base = os.path.join(playbook_dir, f'roles/{var_prefix}')
            watched_files.append(os.path.join(role_base, 'defaults/main.yml'))
            watched_files.extend(self._find_task_files(os.path.join(role_base, 'tasks')))

        extra_var_keys = myvars.get('__extra_var_keys__', [])
        skip_cache = primary_var in extra_var_keys or fallback_var in extra_var_keys

        if not skip_cache:
            result = self._get_cached_result(cache_path, watched_files, primary_var, omit)
            if result is not None:
                return [result]

            result = self._get_cached_result(cache_path, watched_files, fallback_var, omit)
            if result is not None:
                return [result]

        try:
            result = self._get_var(primary_var, myvars)
            if not skip_cache:
                self._save_cache_result(cache_path, watched_files, primary_var, result, omit)
        except AnsibleUndefinedVariable:
            try:
                result = self._get_var(fallback_var, myvars)
                if not skip_cache:
                    self._save_cache_result(cache_path, watched_files, fallback_var, result, omit)
            except AnsibleUndefinedVariable:
                result = default if default is not None else omit

        return [result]

    def _get_var(self, var_name, myvars):
        if not isinstance(var_name, string_types):
            raise AnsibleError(f'Invalid variable name: {var_name}')

        if var_name in myvars:
            return self._templar.template(myvars[var_name], fail_on_undefined=False)

        if 'hostvars' in myvars and 'inventory_hostname' in myvars:
            hostvars = myvars['hostvars'].get(myvars['inventory_hostname'], {})
            if var_name in hostvars:
                return self._templar.template(hostvars[var_name], fail_on_undefined=False)

        raise AnsibleUndefinedVariable(f'Variable "{var_name}" is undefined')

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

        except Exception as e:
            raise AnsibleError(f"Failed to write cache to {cache_path}: {e}")

    def _find_task_files(self, tasks_dir):
        found = []
        for root, _, files in os.walk(tasks_dir):
            for file in files:
                if file.endswith(('.yml', '.yaml')):
                    found.append(os.path.join(root, file))
        return found
