#!/usr/bin/python

# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: migrate_folder
short_description: Migrate a directory from one location to another
description:
    - Ensures a directory exists at the new location, migrating from old location if needed.
    - If the legacy path exists, it is moved to the new path.
    - If neither path exists, the new path is created as a directory.
    - Ensures the final directory at the new path has the specified owner, group, and mode.
    - Errors if both old and new locations exist to prevent potential data merging issues or loss.
    - Errors if the legacy path exists but is not a directory.
    - Errors if the new path exists but is not a directory.
options:
    legacy_path:
        description: Old directory path to migrate from.
        required: true
        type: str
    new_path:
        description: New directory path to migrate to or create.
        required: true
        type: str
    owner:
        description: Name of the user that should own the directory.
        required: false
        type: str
        default: null
    group:
        description: Name of the group that should own the directory.
        required: false
        type: str
        default: null # Keep default null
    mode:
        description: >
            The permissions the resulting directory should have.
            Should be specified as an octal number string (e.g., '0775').
        required: false
        type: str
        default: '0775'
    recurse:
        description: >
            Recursively set ownership for contents of the directory.
            Note: This module primarily manages the top-level directory.
            Recursive ownership is applied via 'chown -R' logic if owner/group changes.
            Mode changes are NOT applied recursively by this module.
        required: false
        type: bool
        default: false
author:
    - Salty
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
    description: Whether the directory was moved from legacy_path to new_path.
    type: bool
    returned: always
    sample: false
created:
    description: Whether a new directory was created at new_path.
    type: bool
    returned: always
    sample: true
path:
    description: The final path of the directory managed.
    type: str
    returned: always
    sample: /opt/error-pages
uid:
    description: Numerical user ID of the owner.
    type: int
    returned: on success
    sample: 1001
gid:
    description: Numerical group ID of the group.
    type: int
    returned: on success
    sample: 1001
mode:
    description: Permissions of the directory as an octal string.
    type: str
    returned: on success
    sample: '0775'
'''

import os
import pwd
import grp
import stat
import traceback
from typing import Any, Optional, Tuple

from ansible.module_utils.basic import AnsibleModule


# Helper to safely get UID/GID
def get_id_info(module: AnsibleModule, owner: Optional[str] = None, group: Optional[str] = None) -> Tuple[int, int]:
    uid = -1
    gid = -1
    if owner is not None:
        try:
            uid = pwd.getpwnam(owner).pw_uid
        except KeyError:
            module.fail_json(msg=f"User '{owner}' not found on the system.")
    if group is not None:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            module.fail_json(msg=f"Group '{group}' not found on the system.")
    return uid, gid

# Helper to validate and convert mode
def validate_mode(module: AnsibleModule, mode_str: Optional[str]) -> Optional[int]:
    if mode_str is None:
        return None
    try:
        # Ensure it's treated as octal
        mode_value = mode_str
        if not isinstance(mode_value, str):
            mode_value = str(mode_value)
        if not mode_value.startswith('0'):
            mode_value = '0' + mode_value  # Ensure octal interpretation for int()
        return int(mode_value, 8)
    except (ValueError, TypeError):
        module.fail_json(msg=f"Invalid mode '{mode_str}' specified. Must be an octal number string (e.g., '0775').")
        return None  # This line is unreachable but satisfies type checker


def run_module() -> None:
    module_args = dict(
        legacy_path=dict(type='str', required=True),
        new_path=dict(type='str', required=True),
        owner=dict(type='str', required=False, default=None),
        group=dict(type='str', required=False, default=None),
        mode=dict(type='str', required=False, default='0775'),
        recurse=dict(type='bool', required=False, default=False)
    )

    result: dict[str, Any] = dict(
        changed=False,
        moved=False,
        created=False,
        path=None,
        uid=None,
        gid=None,
        mode=None,
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    legacy_path = module.params['legacy_path']
    new_path = module.params['new_path']
    owner = module.params['owner']
    group = module.params['group']
    mode_str = module.params['mode']
    recurse = module.params['recurse']

    result['path'] = new_path

    # --- Validation ---
    mode_int = validate_mode(module, mode_str)
    uid, gid = get_id_info(module, owner, group)

    # Check path statuses and types
    legacy_exists = os.path.lexists(legacy_path)
    new_exists = os.path.lexists(new_path)

    if legacy_exists and not os.path.isdir(legacy_path):
        module.fail_json(msg=f"Legacy path '{legacy_path}' exists but is not a directory.")

    if new_exists and not os.path.isdir(new_path):
        module.fail_json(msg=f"New path '{new_path}' exists but is not a directory.")

    legacy_is_dir = legacy_exists and os.path.isdir(legacy_path)
    new_is_dir = new_exists and os.path.isdir(new_path)

    # --- Check Mode Early Exit ---
    if module.check_mode:
        # Predict changes
        if legacy_exists and new_exists:
            # This is an error condition, but check mode shouldn't fail
            result['skipped'] = True
            result['msg'] = f"Both paths exist ({legacy_path}, {new_path}). Migration cannot proceed."
            module.exit_json(**result)

        changed = False
        if legacy_is_dir and not new_exists:
            changed = True # Will move
            result['moved'] = True
        elif not legacy_exists and not new_exists:
            changed = True # Will create
            result['created'] = True
        else: # new_path exists, legacy_path doesn't
             # Check if attributes need changing
            try:
                current_stat = os.stat(new_path)
                current_mode = stat.S_IMODE(current_stat.st_mode)
                if (uid != -1 and current_stat.st_uid != uid) or \
                   (gid != -1 and current_stat.st_gid != gid) or \
                   (mode_int is not None and current_mode != mode_int):
                    changed = True
            except OSError:
                 # Handle case where stat fails (e.g. permissions) - assume change needed
                 changed = True

        result['changed'] = changed
        module.exit_json(**result)

    # --- Main Logic ---

    # Error if both paths exist (safer default)
    if legacy_is_dir and new_is_dir:
        module.fail_json(
            msg=f"Both legacy path '{legacy_path}' and new path '{new_path}' exist as directories. "
                "Cannot safely migrate. Please remove one before proceeding.",
            **result
        )

    # Scenario 1: Migrate (Move)
    if legacy_is_dir and not new_exists:
        try:
            # Ensure all parent directories exist before moving
            parent_dir = os.path.dirname(new_path)

            if parent_dir and not os.path.exists(parent_dir):
                # Find which directories we'll need to create
                path_to_create = parent_dir
                dirs_to_create: list[str] = []

                while path_to_create and path_to_create != '/' and path_to_create != '' and not os.path.exists(path_to_create):
                    dirs_to_create.append(path_to_create)
                    path_to_create = os.path.dirname(path_to_create)

                # Create parent directories
                os.makedirs(parent_dir, exist_ok=True)

                # Apply ownership and permissions only to directories we just created
                if (owner is not None or group is not None or mode_int is not None) and dirs_to_create:
                    for created_dir in dirs_to_create:
                        if os.path.exists(created_dir):
                            try:
                                if owner is not None or group is not None:
                                    current_stat = os.stat(created_dir)
                                    os.chown(created_dir,
                                            uid if uid != -1 else current_stat.st_uid,
                                            gid if gid != -1 else current_stat.st_gid)
                                if mode_int is not None:
                                    os.chmod(created_dir, mode_int)
                            except OSError as e:
                                module.warn(f"Could not set attributes on created parent directory {created_dir}: {str(e)}")
            
            module.atomic_move(legacy_path, new_path)
            result['moved'] = True
            result['changed'] = True
        except (OSError, IOError) as e:
            module.fail_json(msg=f"Failed to move directory '{legacy_path}' to '{new_path}': {str(e)}\n{traceback.format_exc()}", **result)
        # Update state after move
        new_exists = True
        new_is_dir = True

    # Scenario 2: Create
    elif not legacy_exists and not new_exists:
        try:
            os.makedirs(new_path) # Create intermediate dirs if needed
            # Apply initial mode during creation if possible (will be enforced later anyway)
            if mode_int is not None:
                try:
                    os.chmod(new_path, mode_int)
                except OSError as e:
                    # Don't fail here, set_fs_attributes handles final state
                    module.warn(f"Could not set initial mode on created directory {new_path}: {str(e)}")

            result['created'] = True
            result['changed'] = True
        except OSError as e:
            module.fail_json(msg=f"Failed to create directory '{new_path}': {str(e)}\n{traceback.format_exc()}", **result)
        # Update state after create
        new_exists = True
        new_is_dir = True

    # Scenario 3: Target already exists (legacy doesn't) - ensure attributes
    elif new_is_dir and not legacy_exists:
        # Nothing to move or create, but attributes need checking/setting
        pass

    # --- Ensure Final State (Attributes) ---
    if new_is_dir: # Only proceed if the target exists as a directory now
        # Prepare args for setting attributes - start with common file arguments
        file_args = module.load_file_common_arguments(module.params)
        file_args['path'] = new_path
        
        # Only set attributes that were explicitly provided by the user
        if owner is not None:
            file_args['owner'] = owner
        if group is not None:
            file_args['group'] = group
        if mode_str is not None:
            file_args['mode'] = mode_str
        
        # Explicitly disable SELinux context handling and file attributes
        file_args['secontext'] = None
        file_args['selevel'] = None
        file_args['serole'] = None
        file_args['setype'] = None
        file_args['seuser'] = None
        file_args['attributes'] = None
        
        # Handle recursive ownership
        if recurse and (owner is not None or group is not None):
            file_args['recurse'] = True

        # Let Ansible handle idempotency and setting attributes
        try:
            changed_attributes = module.set_fs_attributes_if_different(file_args, result['changed'])
            result['changed'] = result['changed'] or changed_attributes
        except Exception as e:
            module.fail_json(msg=f"Failed to set attributes on {new_path}: {str(e)}", **result)

        # Fetch final state for return values
        try:
            final_stat = os.stat(new_path)
            result['uid'] = final_stat.st_uid
            result['gid'] = final_stat.st_gid
            result['mode'] = format(stat.S_IMODE(final_stat.st_mode), '04o') # Format mode as octal string
        except OSError as e:
            module.warn(f"Could not stat final path {new_path} to retrieve final attributes: {str(e)}")

    # --- Exit ---
    module.exit_json(**result)

def main() -> None:
    run_module()

if __name__ == '__main__':
    main()
