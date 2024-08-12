#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import configparser
import os
import pwd
import grp

def get_file_path(role):
    return f"/opt/saltbox/{role}.ini"

def load_and_save_facts(file_path, instance, keys, owner, group, mode):
    config = configparser.ConfigParser()
    config.read(file_path)

    facts = {}
    changed = False
    if not config.has_section(instance):
        config.add_section(instance)
        changed = True

    for key, default_value in keys.items():
        if config.has_option(instance, key):
            facts[key] = config[instance].get(key)
        else:
            facts[key] = default_value
            config.set(instance, key, default_value)
            changed = True

    if changed:
        with open(file_path, 'w') as configfile:
            config.write(configfile)
        
        os.chmod(file_path, mode)
        os.chown(file_path, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)

    return facts, changed

def delete_facts(file_path, delete_type, instance, keys):
    config = configparser.ConfigParser()
    config.read(file_path)
    changed = False

    if delete_type == 'role' and os.path.exists(file_path):
        os.remove(file_path)
        changed = True
    elif delete_type == 'instance' and config.has_section(instance):
        config.remove_section(instance)
        changed = True
    elif delete_type == 'key' and config.has_section(instance):
        for key in keys:
            if config.has_option(instance, key):
                config.remove_option(instance, key)
                changed = True

    if changed:
        with open(file_path, 'w') as configfile:
            config.write(configfile)

    return changed

def parse_mode(mode):
    if not isinstance(mode, str):
        raise ValueError("Mode must be a quoted string to comply with YAML best practices.")
    mode = mode.strip()
    if mode.startswith('0'):
        try:
            return int(mode, 8)
        except ValueError:
            raise ValueError(f"Invalid octal mode: {mode}")
    else:
        raise ValueError("Mode must be a quoted octal number starting with '0' (e.g., '0644').")

def run_module():
    module_args = dict(
        role=dict(type='str', required=True),
        instance=dict(type='str', required=True),
        method=dict(type='str', choices=['load', 'save', 'delete'], required=False, default='save'),
        keys=dict(type='dict', required=False, default={}),
        delete_type=dict(type='str', choices=['role', 'instance', 'key'], required=False),
        owner=dict(type='str', required=False),
        group=dict(type='str', required=False),
        mode=dict(type='str', required=False, default='0644')
    )

    result = dict(
        changed=False,
        message='',
        facts={}
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
    owner = module.params.get('owner') or module.params['ansible_user_id']
    group = module.params.get('group') or module.params['ansible_user_id']

    try:
        mode = parse_mode(module.params['mode'])
    except ValueError as e:
        module.fail_json(msg=str(e))

    file_path = get_file_path(role)

    if method == 'delete':
        if not delete_type:
            module.fail_json(msg="delete_type is required for delete method.")
        result['changed'] = delete_facts(file_path, delete_type, instance, keys)
    else:
        result['facts'], result['changed'] = load_and_save_facts(file_path, instance, keys, owner, group, mode)

    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
