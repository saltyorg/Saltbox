#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import configparser
import os

def get_file_path(role):
    return f"/opt/saltbox/{role}.ini"

def load_facts(file_path, instance, keys):
    config = configparser.ConfigParser()
    config.read(file_path)
    facts = {key: config[instance].get(key) if config.has_option(instance, key) else "saltbox_fact_missing" for key in keys}
    return facts

def save_facts(file_path, instance, keys):
    config = configparser.ConfigParser()
    config.read(file_path)
    if not config.has_section(instance):
        config.add_section(instance)
    for key, value in keys.items():
        config.set(instance, key, value)
    with open(file_path, 'w') as configfile:
        config.write(configfile)

def delete_facts(file_path, delete_type, role, instance, keys):
    config = configparser.ConfigParser()
    config.read(file_path)
    if delete_type == 'role':
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
    elif delete_type == 'instance':
        if config.has_section(instance):
            config.remove_section(instance)
            with open(file_path, 'w') as configfile:
                config.write(configfile)
            return True
    elif delete_type == 'key':
        if config.has_section(instance):
            changed = False
            for key in keys:
                if config.has_option(instance, key):
                    config.remove_option(instance, key)
                    changed = True
            if changed:
                with open(file_path, 'w') as configfile:
                    config.write(configfile)
            return changed
    return False

def run_module():
    module_args = dict(
        role=dict(type='str', required=True),
        instance=dict(type='str', required=False, default=''),
        method=dict(type='str', choices=['load', 'save', 'delete'], required=True),
        keys=dict(type='dict', required=False, default={}),
        delete_type=dict(type='str', choices=['role', 'instance', 'key'], required=False)
    )

    result = dict(
        changed=False,
        message=''
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    role = module.params['role']
    instance = module.params['instance']
    method = module.params['method']
    keys = module.params['keys']
    delete_type = module.params.get('delete_type')

    file_path = get_file_path(role)

    if method == 'delete':
        if not delete_type:
            module.fail_json(msg="delete_type is required for method 'delete'.")
        elif delete_type == 'role' and not role:
            module.fail_json(msg="Role is required for delete_type 'role'.")
        elif delete_type == 'instance' and not instance:
            module.fail_json(msg="Instance is required for delete_type 'instance'.")
        elif delete_type == 'key' and (not keys):
            module.fail_json(msg="Keys are required for delete_type 'key'.")
        result['changed'] = delete_facts(file_path, delete_type, role, instance, keys)
    elif method == 'load':
        if not instance:
            module.fail_json(msg="Instance is required for method 'load'.")
        if not keys:
            module.fail_json(msg="Keys are required for method 'load'.")
        result['facts'] = load_facts(file_path, instance, list(keys.keys()))
    elif method == 'save':
        if not instance:
            module.fail_json(msg="Instance is required for method 'save'.")
        save_facts(file_path, instance, keys)
        result['changed'] = True
    else:
        module.fail_json(msg=f"Unsupported method: {method}")

    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
