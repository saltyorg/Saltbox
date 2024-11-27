#!/usr/bin/python

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: migrate_folder
short_description: Migrate a directory from one location to another
description:
    - Ensures a directory exists at the new location, migrating from old location if needed
    - Errors if both old and new locations exist to prevent data loss
options:
    legacy_path:
        description: Old directory path to migrate from
        required: true
        type: str
    new_path:
        description: New directory path to migrate to or create
        required: true
        type: str
    owner:
        description: Owner of the directory
        required: false
        type: str
    group:
        description: Group of the directory
        required: false
        type: str
    mode:
        description: Mode of the directory
        required: false
        type: str
        default: '0775'
    recurse:
        description: Apply ownership recursively to contents
        required: false
        type: bool
        default: false
'''

EXAMPLES = r'''
- name: Migrate error pages directory with recursive ownership
  migrate_folder:
    legacy_path: /opt/error_pages
    new_path: /opt/error-pages
    owner: www-data
    group: www-data
    mode: '0775'
    recurse: true
'''

RETURN = r'''
moved:
    description: Whether the directory was moved from legacy to new path
    type: bool
    returned: always
created:
    description: Whether a new directory was created
    type: bool
    returned: always
'''

import os
import shutil
import subprocess
from ansible.module_utils.basic import AnsibleModule

def run_module():
    module_args = dict(
        legacy_path=dict(type='str', required=True),
        new_path=dict(type='str', required=True),
        owner=dict(type='str', required=False),
        group=dict(type='str', required=False),
        mode=dict(type='str', required=False, default='0775'),
        recurse=dict(type='bool', required=False, default=False)
    )

    result = dict(
        changed=False,
        moved=False,
        created=False
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    legacy_path = module.params['legacy_path']
    new_path = module.params['new_path']
    owner = module.params['owner']
    group = module.params['group']
    mode = module.params['mode']
    recurse = module.params['recurse']

    legacy_exists = os.path.exists(legacy_path)
    new_exists = os.path.exists(new_path)

    # Error if both paths exist
    if legacy_exists and new_exists:
        module.fail_json(
            msg=f"Both paths exist: {legacy_path} and {new_path}. Cannot safely migrate.",
            **result
        )

    if module.check_mode:
        result['changed'] = legacy_exists or not new_exists
        return result

    # Move legacy directory if it exists
    if legacy_exists:
        try:
            shutil.move(legacy_path, new_path)
            result['moved'] = True
            result['changed'] = True
        except Exception as e:
            module.fail_json(msg=f"Failed to move directory: {str(e)}", **result)

    # Create new directory if neither exists
    if not legacy_exists and not new_exists:
        try:
            os.makedirs(new_path)
            result['created'] = True
            result['changed'] = True
        except Exception as e:
            module.fail_json(msg=f"Failed to create directory: {str(e)}", **result)

    # Set ownership if specified
    if result['changed'] and (owner or group):
        ownership = f"{owner}:{group}" if owner and group else owner or group
        try:
            cmd = ['chown']
            if recurse:
                cmd.append('-R')
            cmd.append(ownership)
            cmd.append(new_path)
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError as e:
            module.fail_json(msg=f"Failed to set ownership: {str(e)}", **result)

    # Set mode if changed
    if result['changed']:
        try:
            os.chmod(new_path, int(mode, 8))
        except Exception as e:
            module.fail_json(msg=f"Failed to set permissions: {str(e)}", **result)

    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
