#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: qbittorrent_password_hash
short_description: Generates a password hash compatible with qBittorrent.
description:
    - Takes a plain text password and generates a salted hash using the PBKDF2-HMAC-SHA512 algorithm.
    - Uses 100,000 iterations and a 16-byte random salt, matching qBittorrent's expected format.
    - The output format is "@ByteArray(SALT_BASE64:HASH_BASE64)".
    - This module is useful for generating password hashes to be placed in qBittorrent configuration files non-interactively.
    - The input password parameter has `no_log=True` set for security.
options:
    password:
        description: The plain text password to hash.
        type: str
        required: true
        no_log: true
author:
    - Salty
'''

EXAMPLES = r'''
- name: Generate qBittorrent password hash
  qbittorrent_passwd:
    password: "supersecretpassword"
  register: qbit_hash_result

- name: Display the generated hash
  debug:
    var: qbit_hash_result.hash
'''

RETURN = r'''
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
'''

import base64
import hashlib
import os
import traceback
from ansible.module_utils.basic import AnsibleModule


def generate_qbittorrent_hash(plain_passwd: str) -> str:
    """
    Generates a qBittorrent compatible password hash (PBKDF2-HMAC-SHA512).
    """
    ITERATIONS = 100_000  # Standard iteration count for qBittorrent
    SALT_SIZE = 16        # Standard salt size (bytes)

    try:
        salt = os.urandom(SALT_SIZE)
        # Ensure password is bytes for hashing
        password_bytes = plain_passwd.encode()
        
        # Generate the hash
        derived_key = hashlib.pbkdf2_hmac(
            hash_name='sha512',
            password=password_bytes,
            salt=salt,
            iterations=ITERATIONS
        )
        
        # Encode salt and hash in Base64
        salt_b64 = base64.b64encode(salt).decode()
        hash_b64 = base64.b64encode(derived_key).decode()
        
        # Format according to qBittorrent's expectation
        return f"@ByteArray({salt_b64}:{hash_b64})"

    except Exception as e:
        # Wrap underlying exception for better debugging upstream
        raise ValueError(f"Error generating password hash: {str(e)}\n{traceback.format_exc()}")


def main():
    module_args = dict(
        password=dict(type='str', required=True, no_log=True)
    )

    result = dict(
        changed=False,
        hash=None,
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False
    )

    plain_password = module.params['password']

    try:
        # Generate the hash using the dedicated function
        generated_hash = generate_qbittorrent_hash(plain_password)
        result['hash'] = generated_hash
    
    except ValueError as err:
        # If the hashing function raised ValueError, fail the module
        module.fail_json(msg=f"Failed to generate qBittorrent hash: {str(err)}", **result)
    except Exception as e:
         # Catch any other unexpected errors during execution
         module.fail_json(msg=f"An unexpected error occurred: {str(e)}", **result)


    # Exit successfully, returning the hash
    module.exit_json(**result)

if __name__ == '__main__':
    main()
