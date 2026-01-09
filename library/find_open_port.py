#!/usr/bin/python

from ansible.module_utils.basic import AnsibleModule
import subprocess

DOCUMENTATION = """
---
module: find_open_port
short_description: Find an available port in a given range
description:
    - "This module finds an available port between a low and high bound."
options:
    low_bound:
        description:
            - The lowest port to consider
        required: true
    high_bound:
        description:
            - The highest port to consider
        required: true
    protocol:
        description:
            - The protocol to consider: tcp, udp, or both
        required: false
        default: both
"""

EXAMPLES = """
- name: Find an available port
  find_open_port:
    low_bound: 5000
    high_bound: 6000
    protocol: tcp
"""

def find_port(module, low_bound, high_bound, protocol):
    try:
        if low_bound < 1:
            module.fail_json(msg="Low bound must be at least 1")
        if high_bound > 65535:
            module.fail_json(msg="High bound must be at most 65535")
        if high_bound <= low_bound:
            module.fail_json(msg="High bound must be higher than low bound")

        # Generate sequence
        seq = set(range(low_bound, high_bound + 1))

        # Determine command based on protocol
        if protocol == 'tcp':
            cmd = "ss -Htan"
            awk_cmd = "awk '{print $4}'"
            state_filter = "grep LISTEN"
        elif protocol == 'udp':
            cmd = "ss -Huan"
            awk_cmd = "awk '{print $4}'"
            state_filter = ""
        else:  # both
            cmd = "ss -Htuan"
            awk_cmd = "awk '{print $5}'"
            state_filter = "grep -E '^(udp|tcp +LISTEN)'"

        cmd_parts = [cmd]
        if state_filter:
            cmd_parts.append(state_filter)
        cmd_parts.append(awk_cmd)
        cmd_parts.append("grep -Eo '[0-9]+$'")
        cmd_parts.append("sort -u")
        cmd = " | ".join(cmd_parts)

        # Run command to get ports in use
        ports_in_use = subprocess.check_output(cmd, shell=True)
        ports_in_use = set(int(port) for port in ports_in_use.decode().split())

        # Find available ports
        available_ports = seq - ports_in_use

        # Check if there's at least one available port
        if available_ports:
            candidate = min(available_ports)
            return False, {"port": candidate}
        else:
            return True, {"msg": "No available port found in the specified range"}

    except subprocess.CalledProcessError as e:
        module.fail_json(msg=f"Failed to execute ss command: {e}")
    except ValueError as e:
        module.fail_json(msg=f"Failed to parse port numbers: {e}")

def main():
    module = AnsibleModule(
        argument_spec=dict(
            low_bound=dict(type='int', required=True),
            high_bound=dict(type='int', required=True),
            protocol=dict(type='str', default='both', choices=['tcp', 'udp', 'both']),
        ),
        supports_check_mode=True
    )

    is_error, result = find_port(module, module.params['low_bound'], module.params['high_bound'], module.params['protocol'])

    if not is_error:
        module.exit_json(changed=False, meta=result)
    else:
        module.fail_json(msg="Error finding port", meta=result)


if __name__ == '__main__':
    main()
