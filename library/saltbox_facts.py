# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
---
module: saltbox_facts
description:
    - Loads, saves, or deletes configuration facts for Saltbox roles.
    - By default, loads existing values and only saves new keys if they do not exist.
author: salty
options:
    role:
        description:
            - Name of the role.
        required: true
        type: str
    instance:
        description:
            - Instance name for the role.
        required: true
        type: str
    method:
        description:
            - Operation to perform.
            - Use C(delete) to remove facts; omit to save/load.
        required: false
        type: str
        choices:
            - delete
    keys:
        description:
            - Dictionary of key/value pairs to save or delete.
        required: false
        type: dict
        default: {}
    delete_type:
        description:
            - Type of deletion to perform when C(method=delete).
        required: false
        type: str
        choices:
            - role
            - instance
            - key
    owner:
        description:
            - File owner for the facts file.
            - Defaults to the current user when omitted.
        required: false
        type: str
    group:
        description:
            - File group for the facts file.
            - Defaults to the current user's primary group when omitted.
        required: false
        type: str
    mode:
        description:
            - File mode as a quoted octal string (e.g., '0640').
        required: false
        type: str
        default: "0640"
    overwrite:
        description:
            - Whether to overwrite existing values instead of preserving them.
        required: false
        type: bool
        default: false
    base_path:
        description:
            - Base directory path for storing configuration files.
        required: true
        type: str
"""

EXAMPLES = """
- name: Save facts for role
  saltbox_facts:
    role: myapp
    instance: instance1
    keys:
      key1: value1
      key2: value2
    owner: user1
    group: group1
    mode: "0640"
    base_path: "{{ server_appdata_path }}"
  register: register_var

- name: Save facts with overwrite
  saltbox_facts:
    role: myapp
    instance: instance1
    keys:
      key1: value1
      key2: value2
    base_path: "{{ server_appdata_path }}"
    overwrite: true
  register: register_var

- name: Delete specific keys from instance
  saltbox_facts:
    role: myapp
    instance: instance1
    method: delete
    delete_type: key
    keys:
      key1: ""
      key2: ""
    base_path: "{{ server_appdata_path }}"

- name: Delete instance
  saltbox_facts:
    role: myapp
    instance: instance1
    method: delete
    delete_type: instance
    base_path: "{{ server_appdata_path }}"

- name: Delete role
  saltbox_facts:
    role: myapp
    instance: instance1
    method: delete
    delete_type: role
    base_path: "{{ server_appdata_path }}"

- name: Save facts with defaults
  saltbox_facts:
    role: myapp
    instance: instance1
    keys:
      key1: value1
    base_path: "{{ server_appdata_path }}"
  register: register_var

- name: Save facts with custom permissions
  saltbox_facts:
    role: myapp
    instance: instance1
    keys:
      key1: value1
    mode: "0600"
    base_path: "{{ server_appdata_path }}"
  register: register_var
"""

RETURN = """
facts:
    description: Dictionary containing the loaded or saved facts
    type: dict
    returned: When method is 'save' or when keys are processed
changed:
    description: Whether any changes were made
    type: bool
    returned: always
message:
    description: Informational or error message
    type: str
    returned: when applicable
"""

import configparser
import grp
import os
import pwd
import stat
import tempfile
from io import StringIO
from typing import Any

from ansible.module_utils.basic import AnsibleModule


def create_config_parser() -> configparser.ConfigParser:
    """
    Create a consistently configured, case-sensitive INI parser.
    """
    config = configparser.ConfigParser(
        interpolation=None,
        comment_prefixes=('#',),
        inline_comment_prefixes=None,
        default_section='DEFAULT',
        delimiters=('=',),
        empty_lines_in_values=False
    )
    config.optionxform = str
    return config


def read_config(file_path: str) -> configparser.ConfigParser:
    """
    Read an INI file while surfacing filesystem and parsing errors.
    """
    config = create_config_parser()
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as config_file:
                config.read_file(config_file)
        except configparser.Error as error:
            raise ValueError(f"Configuration parsing error in '{file_path}': {error}") from error
        except OSError as error:
            raise OSError(f"Unable to read configuration file '{file_path}': {error}") from error
    return config


def validate_instance_name(instance: Any) -> None:
    """
    Validate that the instance name is a string.

    Args:
        instance: Value to validate as instance name

    Raises:
        ValueError: If instance is not a string
    """
    if not isinstance(instance, str):
        raise ValueError("Instance name must be a string")
    if not instance.strip():
        raise ValueError("Instance name must be non-empty")
    if instance == configparser.DEFAULTSECT:
        raise ValueError(f"Instance name must not be '{configparser.DEFAULTSECT}'")
    if any(character in instance for character in ('\r', '\n', '[', ']')):
        raise ValueError("Instance name must not contain line breaks or square brackets")


def validate_key_name(key: Any) -> None:
    """
    Validate that a key can be represented without changing its INI identity.
    """
    if not isinstance(key, str):
        raise ValueError(f"Invalid key '{key}': must be a string")
    if not key.strip():
        raise ValueError("Configuration keys must be non-empty")
    if any(character in key for character in ('\r', '\n', '=')):
        raise ValueError(f"Invalid key '{key}': must not contain line breaks or '='")
    if key.lstrip().startswith('#'):
        raise ValueError(f"Invalid key '{key}': must not be interpreted as a comment")


def validate_keys(keys: Any, validate_values: bool = True) -> None:
    """
    Validate configuration keys and values.

    Args:
        keys (dict): Dictionary of configuration keys and values to validate

    Raises:
        ValueError: If keys is not a dictionary or if any key/value is invalid
    """
    if not isinstance(keys, dict):
        raise ValueError("Keys must be a dictionary")

    for key, value in keys.items():
        validate_key_name(key)
        if validate_values and not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                f"Invalid value type for key '{key}': must be string, number, or boolean"
            )


def get_file_path(role: str, base_path: str) -> str:
    """
    Get the configuration file path for a role.

    Args:
        role (str): Name of the role
        base_path (str): Base directory path

    Returns:
        str: Full path to the configuration file

    Raises:
        ValueError: If role is not a string
    """
    if not isinstance(role, str):
        raise ValueError("Role name must be a string")
    if os.path.sep in role or (os.path.altsep and os.path.altsep in role):
        raise ValueError("Role name must not contain path separators")
    if role in ('.', '..') or role.strip() == '':
        raise ValueError("Role name must be a non-empty name")
    if not isinstance(base_path, str) or not base_path.strip():
        raise ValueError("Base path must be a non-empty string")
    if not os.path.isabs(base_path):
        raise ValueError("Base path must be absolute")
    return os.path.join(os.path.normpath(base_path), 'saltbox', f'{role}.ini')


def atomic_write(file_path: str, content: str, mode: int, uid: int, gid: int) -> None:
    """
    Write content to file atomically with proper permissions.

    Args:
        file_path (str): Path to the target file
        content (str): Content to write to the file
        mode (int): File permissions mode in octal
        uid (int): Numerical ID of the file owner
        gid (int): Numerical ID of the file group

    Raises:
        OSError: If file operations fail
        IOError: If file operations fail
    """
    directory = os.path.dirname(file_path)
    os.makedirs(directory, exist_ok=True)

    temp_fd, temp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8', newline='\n') as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.chown(temp_path, uid, gid)
        os.chmod(temp_path, mode)

        os.replace(temp_path, file_path)
    except Exception:
        if os.path.lexists(temp_path):
            os.unlink(temp_path)
        raise


def ensure_file_attributes(file_path: str, mode: int, uid: int, gid: int) -> bool:
    """
    Ensure ownership and mode without rewriting file contents.
    """
    file_stat = os.stat(file_path)
    changed = False

    if file_stat.st_uid != uid or file_stat.st_gid != gid:
        os.chown(file_path, uid, gid)
        changed = True

    current_mode = stat.S_IMODE(os.stat(file_path).st_mode)
    if current_mode != mode:
        os.chmod(file_path, mode)
        changed = True

    return changed


def load_existing_facts(file_path: str, instance: str) -> dict[str, str]:
    """
    Load existing facts from configuration file for a specific instance.

    Args:
        file_path (str): Path to the configuration file
        instance (str): Name of the instance

    Returns:
        dict: Dictionary of existing facts for the instance

    """
    validate_instance_name(instance)
    config = read_config(file_path)
    existing_facts: dict[str, str] = {}

    if config.has_section(instance):
        for key, value in config._sections[instance].items():
            if value != 'None':
                existing_facts[key] = value

    return existing_facts


def process_facts(
    file_path: str,
    instance: str,
    keys: dict[str, Any],
    uid: int,
    gid: int,
    mode: int,
    overwrite: bool = False
) -> tuple[dict[str, str], bool]:
    """
    Process facts by loading existing values and saving new ones as needed.

    Args:
        file_path (str): Path to the configuration file
        instance (str): Name of the instance
        keys (dict): Dictionary of keys and values to process
        uid (int): Numerical ID of the file owner
        gid (int): Numerical ID of the file group
        mode (int): File permissions mode in octal
        overwrite (bool): If True, overwrite existing values; if False, keep existing values

    Returns:
        tuple: (dict of final facts, bool indicating if changes were made)

    """
    validate_instance_name(instance)
    validate_keys(keys)

    existing_facts = load_existing_facts(file_path, instance)
    final_facts: dict[str, str] = {}
    keys_to_save: dict[str, str] = {}

    if overwrite:
        final_facts.update(existing_facts)
        final_facts.update({key: str(value) for key, value in keys.items()})
        keys_to_save = {key: str(value) for key, value in keys.items()}
    else:
        final_facts.update({key: str(value) for key, value in keys.items()})
        final_facts.update(existing_facts)
        for key, value in keys.items():
            if key not in existing_facts:
                keys_to_save[key] = str(value)

    if not keys_to_save:
        return final_facts, False

    config = read_config(file_path)
    changed = False

    if not config.has_section(instance):
        config.add_section(instance)
        changed = True

    for key, value in keys_to_save.items():
        section_values = config._sections[instance]
        if key not in section_values or section_values[key] != value:
            config.set(instance, key, value)
            changed = True

    if changed:
        with StringIO() as string_buffer:
            config.write(string_buffer)
            config_str = string_buffer.getvalue()

        atomic_write(file_path, config_str, mode, uid, gid)

    return final_facts, changed


def delete_facts(file_path: str, delete_type: str, instance: str, keys: dict[str, Any]) -> bool:
    """
    Delete facts from configuration file.

    Args:
        file_path (str): Path to the configuration file
        delete_type (str): Type of deletion ('role', 'instance', or 'key')
        instance (str): Name of the instance
        keys (dict): Dictionary of keys to delete (used only for delete_type='key')

    Returns:
        bool: True if changes were made, False otherwise

    """
    validate_instance_name(instance)
    validate_keys(keys, validate_values=False)

    if delete_type == 'role':
        if os.path.lexists(file_path):
            os.remove(file_path)
            return True
        return False

    if not os.path.exists(file_path):
        return False

    config = read_config(file_path)
    changed = False

    if delete_type == 'instance':
        changed = config.remove_section(instance)
    elif delete_type == 'key' and config.has_section(instance):
        section_values = config._sections[instance]
        for key in keys:
            if key in section_values:
                changed = config.remove_option(instance, key) or changed

    if changed:
        with StringIO() as string_buffer:
            config.write(string_buffer)
            config_str = string_buffer.getvalue()

        file_stat = os.stat(file_path)
        atomic_write(
            file_path,
            config_str,
            stat.S_IMODE(file_stat.st_mode),
            file_stat.st_uid,
            file_stat.st_gid
        )

    return changed


def parse_mode(mode: Any) -> int:
    """
    Parse and validate file mode.

    Args:
        mode (str): File mode in octal string format (e.g., '0640')

    Returns:
        int: Parsed mode as integer

    Raises:
        ValueError: If mode is invalid or improperly formatted
    """
    if not isinstance(mode, str):
        raise ValueError("Mode must be a quoted string to comply with YAML best practices.")
    mode = mode.strip()
    if mode.startswith('0'):
        try:
            parsed_mode = int(mode, 8)
        except ValueError:
            raise ValueError(f"Invalid octal mode: {mode}")
        if parsed_mode > 0o7777:
            raise ValueError("Mode must not exceed '07777'.")
        return parsed_mode
    else:
        raise ValueError("Mode must be a quoted octal number starting with '0' (e.g., '0640').")


def get_current_identity() -> tuple[str, str]:
    """
    Get the current user and that user's primary group.

    Returns:
        tuple: Current user name and primary group name
    """
    current_user = pwd.getpwuid(os.geteuid())
    current_group = grp.getgrgid(current_user.pw_gid)
    return current_user.pw_name, current_group.gr_name


def resolve_ownership(owner: str, group: str) -> tuple[int, int]:
    """
    Resolve owner and group names before making filesystem changes.
    """
    try:
        uid = pwd.getpwnam(owner).pw_uid
    except KeyError as error:
        raise ValueError(f"User '{owner}' not found on the system") from error
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError as error:
        raise ValueError(f"Group '{group}' not found on the system") from error
    return uid, gid


def run_module() -> None:
    """
    Main module execution.

    This function handles the module's argument parsing, execution flow,
    and return value preparation. It uses AnsibleModule for proper Ansible
    integration.

    The function processes the following parameters:
    - role (str): The role name (required)
    - instance (str): The instance name (required)
    - method (str): Operation to perform ('delete') - save/load is now default behavior
    - keys (dict): Configuration keys and values (default: {})
    - delete_type (str): Type of deletion ('role', 'instance', 'key')
    - owner (str): File owner (default: current user)
    - group (str): File group (default: current user's primary group)
    - mode (str): File mode in octal string format (default: '0640')
    - overwrite (bool): If True, overwrite existing values; if False, keep existing (default: False)
    - base_path (str): Base directory path for storing configuration files (required)
    """
    module_args = dict(
        role=dict(type='str', required=True),
        instance=dict(type='str', required=True),
        method=dict(type='str', choices=['delete'], required=False),
        keys=dict(type='dict', required=False, default={}),
        delete_type=dict(type='str', choices=['role', 'instance', 'key'], required=False),
        owner=dict(type='str', required=False),
        group=dict(type='str', required=False),
        mode=dict(type='str', required=False, default='0640'),
        overwrite=dict(type='bool', required=False, default=False),
        base_path=dict(type='str', required=True)
    )

    result = dict(
        changed=False,
        message='',
        facts={}
    )

    module = AnsibleModule(
        argument_spec=module_args
    )

    try:
        role: str = module.params['role']
        instance: str = module.params['instance']
        method: str | None = module.params.get('method')
        keys: dict[str, Any] = module.params['keys']
        delete_type: str | None = module.params.get('delete_type')
        overwrite: bool = module.params['overwrite']
        base_path: str = module.params['base_path']

        current_user, current_group = get_current_identity()
        owner: str = module.params.get('owner') or current_user
        group: str = module.params.get('group') or current_group

        mode = parse_mode(module.params['mode'])
        file_path = get_file_path(role, base_path)

        if method == 'delete':
            if not delete_type:
                module.fail_json(msg="delete_type is required for delete method.")
            result['changed'] = delete_facts(file_path, delete_type, instance, keys)
        else:
            uid, gid = resolve_ownership(owner, group)
            result['facts'], content_changed = process_facts(
                file_path, instance, keys, uid, gid, mode, overwrite
            )
            attribute_changed = (
                ensure_file_attributes(file_path, mode, uid, gid)
                if os.path.exists(file_path)
                else False
            )
            result['changed'] = content_changed or attribute_changed

        module.exit_json(**result)

    except Exception as error:
        module.fail_json(msg=str(error))


def main() -> None:
    """
    Module entry point.
    """
    run_module()


if __name__ == '__main__':
    main()
