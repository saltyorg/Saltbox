#!/usr/bin/python
# -*- coding: utf-8 -*-

#########################################################################
# Title:         Saltbox: Library | TLD Parse                          #
# Author(s):     salty                                                  #
# URL:           https://github.com/saltyorg/Saltbox                    #
# --                                                                    #
#########################################################################
#                   GNU General Public License v3.0                     #
#########################################################################

from ansible.module_utils.basic import AnsibleModule
from tld import get_tld

DOCUMENTATION = r'''
---
module: tld_parse
short_description: Parse domain names for DNS operations
description:
    - Parses a domain name into components needed for DNS record management
    - Extracts the full domain and subdomain portions
    - Uses the tld Python library for parsing
options:
    url:
        description:
            - The domain or URL to parse
        required: true
        type: str
    record:
        description:
            - Optional DNS record to prepend to the domain
        required: false
        type: str
        default: ''
author:
    - Saltbox Team
'''

EXAMPLES = r'''
- name: Parse domain
  tld_parse:
    url: "{{ user.domain }}"
  register: domain_info

- name: Parse domain with record
  tld_parse:
    url: "{{ user.domain }}"
    record: "subdomain"
  register: domain_info

- name: Use parsed values
  debug:
    msg: "Domain: {{ domain_info.domain }}, Record: {{ domain_info.record }}"
'''

RETURN = r'''
fld:
    description: Full domain name (e.g., example.com)
    type: str
    returned: always
    sample: 'example.com'
subdomain:
    description: Subdomain portion (empty string if none)
    type: str
    returned: always
    sample: 'www'
record:
    description: DNS record format (subdomain or '@' for root domain)
    type: str
    returned: always
    sample: 'www'
tld:
    description: Top-level domain (e.g., com, org, co.uk)
    type: str
    returned: always
    sample: 'com'
domain:
    description: Domain name without TLD (e.g., example)
    type: str
    returned: always
    sample: 'example'
'''


def main():
    module = AnsibleModule(
        argument_spec=dict(
            url=dict(type='str', required=True),
            record=dict(type='str', default='')
        ),
        supports_check_mode=True
    )

    url = module.params['url']
    record = module.params['record']

    try:
        # Build the full URL
        if record:
            full_url = f"http://{record}.{url}"
        else:
            full_url = f"http://{url}"

        # Parse using tld library
        res = get_tld(full_url, as_object=True)

        # Extract components - use same naming as tld library
        fld = res.fld
        subdomain = res.subdomain if res.subdomain else ''
        tld = res.tld
        domain = res.domain

        # Format record for DNS operations
        dns_record = subdomain if subdomain else '@'

        module.exit_json(
            changed=False,
            fld=fld,
            subdomain=subdomain,
            record=dns_record,
            tld=tld,
            domain=domain
        )

    except Exception as e:
        module.fail_json(msg=f"Failed to parse domain: {str(e)}")


if __name__ == '__main__':
    main()
