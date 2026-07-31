# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = """
---
module: cloudflare_dns_records
description:
    - "This module fetches DNS records from Cloudflare for a specific zone and record name."
    - "It supports both API key and API token authentication methods."
author: salty
requirements:
    - cloudflare==5.6.0
options:
    auth_email:
        description:
            - Email associated with Cloudflare account
            - Required when using auth_key authentication
        required: false
        type: str
    auth_key:
        description:
            - API key for Cloudflare
            - Required when using auth_key authentication
        required: false
        type: str
    auth_token:
        description:
            - API token for Cloudflare
            - Can be used instead of auth_email and auth_key
        required: false
        type: str
    zone_name:
        description:
            - Name of the Cloudflare zone (e.g., example.com)
        required: true
        type: str
    record:
        description:
            - Fully qualified DNS record name to fetch (e.g., subdomain.example.com)
            - A trailing DNS dot is accepted and removed before querying Cloudflare
            - C(@) and C(@.example.com) are accepted as zone-apex records
        required: true
        type: str
"""

EXAMPLES = """
# Fetch DNS records using API key
- name: Fetch Cloudflare DNS records with API key
  cloudflare_dns_records:
    auth_email: "user@example.com"
    auth_key: "{{ cloudflare_api_key }}"
    zone_name: "example.com"
    record: "app.example.com"
  register: dns_records

# Fetch DNS records using API token
- name: Fetch Cloudflare DNS records with token
  cloudflare_dns_records:
    auth_token: "{{ cloudflare_api_token }}"
    zone_name: "example.com"
    record: "app.example.com"
  register: dns_records

# Access the records
- name: Display records
  ansible.builtin.debug:
    var: dns_records.records
"""

RETURN = """
records:
    description:
        - List of DNS records matching the query
        - An empty list is returned when no records match
    type: list
    elements: dict
    returned: success
    contains:
        name:
            description: Fully qualified DNS record name
            type: str
            returned: success
        type:
            description: DNS record type
            type: str
            returned: success
        content:
            description: DNS record content
            type: str
            returned: success
        proxied:
            description: Whether Cloudflare proxies the record
            type: bool
            returned: when supported by the record type
zone_id:
    description: The Cloudflare zone ID for the specified zone
    type: str
    returned: success
changed:
    description: Whether any changes were made (always False for this read-only module)
    type: bool
    returned: always
"""

from typing import TYPE_CHECKING

from ansible.module_utils.basic import AnsibleModule

if TYPE_CHECKING:
    from cloudflare import Cloudflare


def normalize_dns_name(value: str, parameter: str, module: AnsibleModule) -> str:
    """
    Normalize a DNS name accepted by the module.

    Surrounding whitespace and one optional terminal DNS dot are removed.
    Empty values are rejected before making a Cloudflare API request.
    """
    normalized = value.strip()
    if normalized.endswith('.'):
        normalized = normalized[:-1]
    if not normalized:
        module.fail_json(msg=f"{parameter} must not be empty")
    return normalized


def normalize_record_name(value: str, zone_name: str, module: AnsibleModule) -> str:
    """
    Normalize a DNS record name, including Cloudflare's apex convention.
    """
    normalized = normalize_dns_name(value, 'record', module)
    if normalized in ('@', f"@.{zone_name}"):
        return zone_name
    return normalized


def get_zone_id(client: "Cloudflare", zone_name: str, module: AnsibleModule) -> str:
    """
    Fetch the zone ID for a given zone name from Cloudflare.

    Args:
        client: Cloudflare client instance
        zone_name (str): Name of the zone to look up
        module: AnsibleModule instance for error reporting

    Returns:
        str: The zone ID

    Raises:
        Calls module.fail_json on error
    """
    try:
        zone = client.zones.list(name=zone_name)
        if len(zone.result) == 0:
            module.fail_json(msg=f"Specified zone '{zone_name}' was not found")
        return zone.result[0].id
    except Exception as e:
        module.fail_json(msg=f"Error fetching zone ID: {str(e)}")


def fetch_dns_records(client: "Cloudflare", zone_id: str, record_name: str, module: AnsibleModule) -> list[dict[str, object]]:
    """
    Fetch DNS records from Cloudflare.

    Args:
        client: Cloudflare client instance
        zone_id (str): The Cloudflare zone ID
        record_name (str): The DNS record name to fetch
        module: AnsibleModule instance for error reporting

    Returns:
        list: List of DNS records

    Raises:
        Calls module.fail_json on error
    """
    try:
        records_response = client.dns.records.list(zone_id=zone_id, name={"exact": record_name})
        if records_response is None:
            module.fail_json(msg="No response from Cloudflare API")

        records: list[dict[str, object]] = []
        for page in records_response.iter_pages():
            records.extend(record.to_dict() for record in page.result)
        return records
    except Exception as e:
        module.fail_json(msg=f"Error fetching DNS records: {str(e)}")


def run_module() -> None:
    """
    Main module execution.

    This function handles the module's argument parsing, execution flow,
    and return value preparation.
    """
    module_args = dict(
        auth_email=dict(type='str', required=False, no_log=False),
        auth_key=dict(type='str', required=False, no_log=True),
        auth_token=dict(type='str', required=False, no_log=True),
        zone_name=dict(type='str', required=True),
        record=dict(type='str', required=True),
    )

    result: dict[str, bool | str | list[dict[str, object]]] = {
        'changed': False,
        'records': [],
        'zone_id': '',
    }

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True,
        required_one_of=[
            ['auth_token', 'auth_key']
        ],
        required_together=[
            ['auth_email', 'auth_key']
        ],
        mutually_exclusive=[
            ['auth_token', 'auth_key']
        ],
    )

    try:
        # Import cloudflare here to provide better error message if not installed
        try:
            from cloudflare import Cloudflare
        except ImportError:
            module.fail_json(msg="The 'cloudflare' Python library is required. Install it with: pip install cloudflare")

        auth_email = module.params.get('auth_email')
        auth_key = module.params.get('auth_key')
        auth_token = module.params.get('auth_token')
        zone_name = normalize_dns_name(module.params['zone_name'], 'zone_name', module)
        record = normalize_record_name(module.params['record'], zone_name, module)

        # Initialize Cloudflare client
        if auth_token:
            cf = Cloudflare(api_token=auth_token)
        else:
            cf = Cloudflare(api_email=auth_email, api_key=auth_key)

        # Fetch zone ID
        zone_id = get_zone_id(cf, zone_name, module)
        result['zone_id'] = zone_id

        # Fetch DNS records
        records = fetch_dns_records(cf, zone_id, record, module)
        result['records'] = records

        module.exit_json(**result)

    except Exception as e:
        module.fail_json(msg=f"Unexpected error: {str(e)}")


def main() -> None:
    """
    Module entry point.
    """
    run_module()


if __name__ == '__main__':
    main()
