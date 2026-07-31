# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
---
module: tld_parse
description:
    - Parses a domain name into components needed for DNS record management
    - Extracts the full domain and subdomain portions
    - Uses the tld Python library for parsing
author: salty
requirements:
    - tld==0.13.2
options:
    url:
        description:
            - The domain or URL to parse
        required: true
        type: str
    record:
        description:
            - Optional DNS record to prepend to the URL hostname
        required: false
        type: str
        default: ''
"""

EXAMPLES = """
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
  ansible.builtin.debug:
    msg: "Domain: {{ domain_info.domain }}, Record: {{ domain_info.record }}"
"""

RETURN = """
fld:
    description: Full domain name (e.g., example.com)
    type: str
    returned: always
    sample: "example.com"
subdomain:
    description: Subdomain portion (empty string if none)
    type: str
    returned: always
    sample: "www"
record:
    description: DNS record format (subdomain or '@' for root domain)
    type: str
    returned: always
    sample: "www"
tld:
    description: Top-level domain (e.g., com, org, co.uk)
    type: str
    returned: always
    sample: "com"
domain:
    description: Domain name without TLD (e.g., example)
    type: str
    returned: always
    sample: "example"
"""

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule

if TYPE_CHECKING:
    from tld.utils import Result


def build_parse_url(url: str, record: str, module: AnsibleModule) -> str:
    """
    Build a URL containing the hostname that should be parsed.

    The input may be a bare domain or a URL. Schemes, ports, paths, queries,
    and fragments are discarded because only the hostname is relevant.
    """
    normalized_url = url.strip()
    if not normalized_url:
        module.fail_json(msg="url must not be empty")

    parsed_url = urlsplit(normalized_url if '://' in normalized_url else f"//{normalized_url}")
    hostname = parsed_url.hostname
    if not hostname:
        module.fail_json(msg=f"Could not extract a hostname from '{url}'")

    if hostname.endswith('.'):
        hostname = hostname[:-1]
    if not hostname:
        module.fail_json(msg=f"Could not extract a hostname from '{url}'")

    normalized_record = record.strip()
    if normalized_record.endswith('.'):
        normalized_record = normalized_record[:-1]
    if normalized_record:
        hostname = f"{normalized_record}.{hostname}"

    return f"http://{hostname}"


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            url=dict(type='str', required=True),
            record=dict(type='str', default='')
        ),
        supports_check_mode=True
    )

    url: str = module.params['url']
    record: str = module.params['record']

    try:
        try:
            from tld import get_tld
        except ImportError:
            module.fail_json(msg="The 'tld' Python library is required. Install it with: pip install tld")

        full_url = build_parse_url(url, record, module)
        res: Result = get_tld(full_url, as_object=True)  # type: ignore[assignment]

        # Extract components - use same naming as tld library
        fld: str = res.fld
        subdomain: str = res.subdomain if res.subdomain else ''
        tld: str = res.tld
        domain: str = res.domain

        # Format record for DNS operations
        dns_record: str = subdomain if subdomain else '@'

        module.exit_json(
            changed=False,
            fld=fld,
            subdomain=subdomain,
            record=dns_record,
            tld=tld,
            domain=domain
        )

    except Exception as e:
        module.fail_json(msg=f"Failed to parse domain: {e!s}")


if __name__ == '__main__':
    main()
