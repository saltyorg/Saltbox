#!/usr/bin/python
"""
Ansible module for managing Saltbox configuration facts.

This module provides functionality to load, save, and delete configuration facts
for Saltbox roles. By default, it loads existing values and only saves new ones
if the keys don't exist yet.

Example Usage:
    # Save facts (loads existing, only saves new keys)
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
      register: register_var

    # Save facts with overwrite (ignores existing values)
    - name: Save facts with overwrite
      saltbox_facts:
        role: myapp
        instance: instance1
        keys:
          key1: value1
          key2: value2
        overwrite: true
      register: register_var

    # Delete specific keys from instance
    - name: Delete specific keys
      saltbox_facts:
        role: myapp
        instance: instance1
        method: delete
        delete_type: key
        keys:
          key1: ""
          key2: ""

    # Delete entire instance
    - name: Delete instance
      saltbox_facts:
        role: myapp
        instance: instance1
        method: delete
        delete_type: instance

    # Delete entire role (removes configuration file)
    - name: Delete role
      saltbox_facts:
        role: myapp
        instance: instance1
        method: delete
        delete_type: role

    # Save with default owner/group (current user)
    - name: Save facts with defaults
      saltbox_facts:
        role: myapp
        instance: instance1
        keys:
          key1: value1
      register: register_var

    # Save with specific file permissions
    - name: Save facts with custom permissions
      saltbox_facts:
        role: myapp
        instance: instance1
        keys:
          key1: value1
        mode: "0600"
      register: register_var

Return Values:
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
    warnings:
        description: List of warning messages
        type: list
        returned: when applicable
"""

from ansible.module_utils.basic import AnsibleModule
import configparser
import os
import pwd
import grp
import tempfile
import shutil
from io import StringIO

def validate_instance_name(instance):
    """
    Validate that the instance name is a string.

    Args:
        instance: Value to validate as instance name

    Raises:
        ValueError: If instance is not a string
    """
    if not isinstance(instance, str):
        raise ValueError("Instance name must be a string")

def validate_keys(keys):
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
        if not isinstance(key, str):
            raise ValueError(f"Invalid key '{key}': must be a string")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                f"Invalid value type for key '{key}': must be string, number, or boolean"
            )

def get_file_path(role):
    """
    Get the configuration file path for a role.

    Args:
        role (str): Name of the role

    Returns:
        str: Full path to the configuration file

    Raises:
        ValueError: If role is not a string
    """
    if not isinstance(role, str):
        raise ValueError("Role name must be a string")
    return f"/opt/saltbox/{role}.ini"

def atomic_write(file_path, content, mode, owner, group):
    """
    Write content to file atomically with proper permissions.

    Args:
        file_path (str): Path to the target file
        content (str): Content to write to the file
        mode (int): File permissions mode in octal
        owner (str): Username of the file owner
        group (str): Group name for the file

    Raises:
        OSError: If file operations fail
        IOError: If file operations fail
    """
    directory = os.path.dirname(file_path)
    os.makedirs(directory, exist_ok=True)
    
    temp_fd, temp_path = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(temp_fd, 'w') as temp_file:
            temp_file.write(content)
        
        os.chmod(temp_path, mode)
        os.chown(temp_path, 
                pwd.getpwnam(owner).pw_uid,
                grp.getgrnam(group).gr_gid)
        
        shutil.move(temp_path, file_path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

def load_existing_facts(file_path, instance):
    """
    Load existing facts from configuration file for a specific instance.

    Args:
        file_path (str): Path to the configuration file
        instance (str): Name of the instance

    Returns:
        dict: Dictionary of existing facts for the instance

    Raises:
        Exception: With detailed error message for various failure scenarios
    """
    try:
        validate_instance_name(instance)
        
        config = configparser.ConfigParser(
            interpolation=None,
            comment_prefixes=('#',),
            inline_comment_prefixes=('#',),
            default_section='DEFAULT',
            delimiters=('=',),
            empty_lines_in_values=False
        )
        
        config.optionxform = str
        
        existing_facts = {}
        
        if os.path.exists(file_path):
            config.read(file_path)
            if config.has_section(instance):
                for key in config.options(instance):
                    if key != 'DEFAULT':  # Skip DEFAULT section items
                        value = config.get(instance, key)
                        if value != 'None':
                            existing_facts[key] = value
                
        return existing_facts
        
    except configparser.Error as e:
        raise Exception(f"Configuration parsing error: {str(e)}")
    except Exception as e:
        raise Exception(f"Unexpected error: {str(e)}")

def process_facts(file_path, instance, keys, owner, group, mode, overwrite=False):
    """
    Process facts by loading existing values and saving new ones as needed.

    Args:
        file_path (str): Path to the configuration file
        instance (str): Name of the instance
        keys (dict): Dictionary of keys and values to process
        owner (str): Username of the file owner
        group (str): Group name for the file
        mode (int): File permissions mode in octal
        overwrite (bool): If True, overwrite existing values; if False, keep existing values

    Returns:
        tuple: (dict of final facts, bool indicating if changes were made)

    Raises:
        Exception: With detailed error message for various failure scenarios
    """
    try:
        validate_instance_name(instance)
        validate_keys(keys)
        
        # Load existing facts first
        existing_facts = load_existing_facts(file_path, instance)
        
        # Determine final facts based on overwrite setting
        final_facts = {}
        keys_to_save = {}
        
        if overwrite:
            # Overwrite mode: use provided keys, keep existing keys not in provided keys
            final_facts.update(existing_facts)
            final_facts.update({k: str(v) for k, v in keys.items()})
            keys_to_save = {k: str(v) for k, v in keys.items()}
        else:
            # Default mode: keep existing values, only add new keys
            final_facts.update({k: str(v) for k, v in keys.items()})
            final_facts.update(existing_facts)  # Existing values override new ones
            
            # Only save keys that don't exist yet
            for key, value in keys.items():
                if key not in existing_facts:
                    keys_to_save[key] = str(value)
        
        # If no new keys to save, return existing facts without changes
        if not keys_to_save:
            return final_facts, False
        
        # Save new/updated keys
        config = configparser.ConfigParser(
            interpolation=None,
            comment_prefixes=('#',),
            inline_comment_prefixes=('#',),
            default_section='DEFAULT',
            delimiters=('=',),
            empty_lines_in_values=False
        )
        
        config.optionxform = str
        
        if os.path.exists(file_path):
            config.read(file_path)

        changed = False
        
        if not config.has_section(instance):
            config.add_section(instance)
            changed = True

        for key, value in keys_to_save.items():
            if (not config.has_section(instance) or 
                not config.has_option(instance, key) or 
                config.get(instance, key) != str(value)):
                config.set(instance, key, str(value))
                changed = True

        if changed:
            with StringIO() as string_buffer:
                config.write(string_buffer)
                config_str = string_buffer.getvalue()
            
            atomic_write(file_path, config_str, mode, owner, group)

        return final_facts, changed
        
    except (OSError, IOError) as e:
        raise Exception(f"File operation error: {str(e)}")
    except configparser.Error as e:
        raise Exception(f"Configuration parsing error: {str(e)}")
    except Exception as e:
        raise Exception(f"Unexpected error: {str(e)}")

def delete_facts(file_path, delete_type, instance, keys):
    """
    Delete facts from configuration file.

    Args:
        file_path (str): Path to the configuration file
        delete_type (str): Type of deletion ('role', 'instance', or 'key')
        instance (str): Name of the instance
        keys (dict): Dictionary of keys to delete (used only for delete_type='key')

    Returns:
        bool: True if changes were made, False otherwise

    Raises:
        Exception: With detailed error message for various failure scenarios
    """
    try:
        if delete_type == 'role':
            if os.path.exists(file_path):
                os.remove(file_path)
                return True
            return False

        if not os.path.exists(file_path):
            return False

        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read(file_path)
        changed = False

        if delete_type == 'instance':
            if config.has_section(instance):
                config.remove_section(instance)
                changed = True
        elif delete_type == 'key' and config.has_section(instance):
            for key in keys:
                if config.has_option(instance, key):
                    config.remove_option(instance, key)
                    changed = True

        if changed:
            with StringIO() as string_buffer:
                config.write(string_buffer)
                config_str = string_buffer.getvalue()
            
            stat = os.stat(file_path)
            atomic_write(file_path, config_str, stat.st_mode, 
                        pwd.getpwuid(stat.st_uid).pw_name,
                        grp.getgrgid(stat.st_gid).gr_name)

        return changed
        
    except (OSError, IOError) as e:
        raise Exception(f"File operation error: {str(e)}")
    except configparser.Error as e:
        raise Exception(f"Configuration parsing error: {str(e)}")
    except Exception as e:
        raise Exception(f"Unexpected error: {str(e)}")

def parse_mode(mode):
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
            return int(mode, 8)
        except ValueError:
            raise ValueError(f"Invalid octal mode: {mode}")
    else:
        raise ValueError("Mode must be a quoted octal number starting with '0' (e.g., '0640').")

def get_current_user():
    """
    Get current user name.

    Returns:
        str: Name of the current user
    """
    return pwd.getpwuid(os.getuid()).pw_name

def run_module():
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
    - group (str): File group (default: current user)
    - mode (str): File mode in octal string format (default: '0640')
    - overwrite (bool): If True, overwrite existing values; if False, keep existing (default: False)
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
        overwrite=dict(type='bool', required=False, default=False)
    )

    result = dict(
        changed=False,
        message='',
        facts={},
        warnings=[]
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    try:
        role = module.params['role']
        instance = module.params['instance']
        method = module.params.get('method')
        keys = module.params['keys']
        delete_type = module.params.get('delete_type')
        overwrite = module.params['overwrite']
        
        current_user = get_current_user()
        owner = module.params.get('owner') or current_user
        group = module.params.get('group') or current_user

        mode = parse_mode(module.params['mode'])
        file_path = get_file_path(role)

        if method == 'delete':
            if not delete_type:
                module.fail_json(msg="delete_type is required for delete method.")
            result['changed'] = delete_facts(file_path, delete_type, instance, keys)
        else:
            # Default behavior: process facts (load existing, save new ones)
            result['facts'], result['changed'] = process_facts(
                file_path, instance, keys, owner, group, mode, overwrite
            )

        module.exit_json(**result)
        
    except Exception as e:
        module.fail_json(msg=str(e))

def main():
    """
    Module entry point.
    """
    run_module()

if __name__ == '__main__':
    main()
