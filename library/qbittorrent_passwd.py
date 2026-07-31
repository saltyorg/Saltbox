# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
---
module: qbittorrent_passwd
description:
    - Takes a plain text password and generates a salted hash using the PBKDF2-HMAC-SHA512 algorithm.
    - Uses 100,000 iterations and a 16-byte random salt, matching qBittorrent's expected format.
    - The output format is "@ByteArray(SALT_BASE64:HASH_BASE64)".
    - This module is useful for generating password hashes to be placed in qBittorrent configuration files non-interactively.
    - The input password parameter has `no_log=True` set for security.
author: salty
options:
    password:
        description: The plain text password to hash.
        type: str
        required: true
        no_log: true
"""

EXAMPLES = """
- name: Generate qBittorrent password hash
  qbittorrent_passwd:
    password: "{{ user.pass }}"
  register: qbit_hash_result
"""

RETURN = """
hash:
    description: The generated qBittorrent-compatible password hash string.
    type: str
    returned: on success
    sample: "@ByteArray(aBcDeFgHiJkLmNoPqRsTuVw==:xYz123AbCdEfGhIjKlMnOpQrStUvWxYz12/abc+def=)"
changed:
    description: Indicates if any state was changed. Always false for this module.
    type: bool
    returned: always
    sample: false
"""

import base64
import hashlib
import os

from ansible.module_utils.basic import AnsibleModule


ITERATIONS = 100_000
SALT_SIZE = 16


def generate_qbittorrent_hash(plain_passwd: str) -> str:
    """
    Generates a qBittorrent compatible password hash (PBKDF2-HMAC-SHA512).
    """
    salt = os.urandom(SALT_SIZE)
    derived_key = hashlib.pbkdf2_hmac(
        hash_name='sha512',
        password=plain_passwd.encode(),
        salt=salt,
        iterations=ITERATIONS
    )
    salt_b64 = base64.b64encode(salt).decode()
    hash_b64 = base64.b64encode(derived_key).decode()
    return f"@ByteArray({salt_b64}:{hash_b64})"


def main() -> None:
    module_args = dict(
        password=dict(type='str', required=True, no_log=True)
    )

    result: dict[str, bool | str] = {
        'changed': False,
        'hash': '',
    }

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    plain_password: str = module.params['password']

    try:
        result['hash'] = generate_qbittorrent_hash(plain_password)
    except Exception as e:
        module.fail_json(msg=f"Failed to generate qBittorrent hash: {str(e)}", **result)

    module.exit_json(**result)

if __name__ == '__main__':
    main()
