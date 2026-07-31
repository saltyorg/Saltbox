# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
---
module: find_open_port
description:
    - "This module finds an available port between a low and high bound."
author: salty
options:
    low_bound:
        description:
            - The lowest port to consider
        required: true
        type: int
    high_bound:
        description:
            - The highest port to consider
        required: true
        type: int
    protocol:
        description:
            - "The protocol to consider: tcp, udp, or both"
        required: false
        default: both
        type: str
"""

EXAMPLES = """
- name: Find an available port
  find_open_port:
    low_bound: 5000
    high_bound: 6000
    protocol: tcp
"""

RETURN = """
meta:
    description: Result metadata including the selected port.
    type: dict
    returned: success
    sample: {"port": 5432}
    contains:
        port:
            description: Lowest observed unused port in the inclusive range.
            type: int
            returned: success
"""

from ansible.module_utils.basic import AnsibleModule


def parse_ports_in_use(output: str, protocol: str) -> set[int]:
    """
    Parse listening TCP ports and all bound UDP ports from ``ss -Htuan`` output.
    """
    ports: set[int] = set()

    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 5:
            raise ValueError(f"Unexpected ss output: {line}")

        socket_protocol = fields[0]
        if socket_protocol not in ('tcp', 'udp'):
            continue
        if protocol != 'both' and socket_protocol != protocol:
            continue
        if socket_protocol == 'tcp' and fields[1] != 'LISTEN':
            continue

        port_text = fields[4].rsplit(':', 1)[-1]
        if not port_text.isdigit():
            raise ValueError(f"Could not parse port from ss output: {line}")

        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError(f"Invalid port in ss output: {port}")
        ports.add(port)

    return ports


def get_ports_in_use(module: AnsibleModule, protocol: str) -> set[int]:
    """
    Fetch TCP and UDP sockets from ``ss``.
    """
    ss_path = module.get_bin_path('ss', required=True)
    rc, stdout, stderr = module.run_command([ss_path, '-Htuan'])
    if rc != 0:
        module.fail_json(msg=f"Failed to execute ss command: {stderr.strip() or stdout.strip()}")

    return parse_ports_in_use(stdout, protocol)


def find_port(module: AnsibleModule, low_bound: int, high_bound: int, protocol: str) -> tuple[bool, dict[str, object]]:
    try:
        if low_bound < 1:
            module.fail_json(msg="Low bound must be at least 1")
        if high_bound > 65535:
            module.fail_json(msg="High bound must be at most 65535")
        if high_bound < low_bound:
            module.fail_json(msg="High bound must be greater than or equal to low bound")

        seq = set(range(low_bound, high_bound + 1))
        ports_in_use = get_ports_in_use(module, protocol)
        available_ports = seq - ports_in_use

        if available_ports:
            candidate = min(available_ports)
            return False, {"port": candidate}
        return True, {"msg": "No available port found in the specified range"}

    except ValueError as e:
        module.fail_json(msg=f"Failed to parse ss output: {e}")

def main() -> None:
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
