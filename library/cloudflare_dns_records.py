#!/usr/bin/python
"""
Ansible module for fetching Cloudflare DNS records.

This module provides functionality to fetch DNS records from Cloudflare
for a specific zone and record name.

Example Usage:
    # Fetch DNS records for a domain
    - name: Fetch Cloudflare DNS records
      cloudflare_dns_records:
        auth_email: "user@example.com"
        auth_key: "api_key_here"
        zone_name: "example.com"
        record: "subdomain.example.com"
      register: dns_records

    # Fetch DNS records using API token (recommended)
    - name: Fetch Cloudflare DNS records with token
      cloudflare_dns_records:
        auth_token: "token_here"
        zone_name: "example.com"
        record: "subdomain.example.com"
      register: dns_records

Return Values:
    records:
        description: List of DNS records matching the query
        type: list
        returned: success
    zone_id:
        description: The Cloudflare zone ID for the specified zone
        type: str
        returned: success
    changed:
        description: Whether any changes were made (always False for this read-only module)
        type: bool
        returned: always
"""

from ansible.module_utils.basic import AnsibleModule
import datetime
import json
from typing import List, cast

DOCUMENTATION = """
---
module: cloudflare_dns_records
short_description: Fetch DNS records from Cloudflare
description:
    - "This module fetches DNS records from Cloudflare for a specific zone and record name."
    - "It supports both API key and API token authentication methods."
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
            - API token for Cloudflare (recommended over auth_key)
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
            - DNS record name to fetch (e.g., subdomain.example.com)
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

# Fetch DNS records using API token (recommended)
- name: Fetch Cloudflare DNS records with token
  cloudflare_dns_records:
    auth_token: "{{ cloudflare_api_token }}"
    zone_name: "example.com"
    record: "app.example.com"
  register: dns_records

# Access the records
- name: Display records
  debug:
    var: dns_records.records
"""


class CustomJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles datetime objects."""
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            return obj.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        return json.JSONEncoder.default(self, obj)


def get_zone_id(client, zone_name, module):
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


def fetch_dns_records(client, zone_id, record_name, module):
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
        records = client.dns.records.list(zone_id=zone_id, name=record_name).to_dict()
        results = cast(List[dict], records["result"])
        return results
    except Exception as e:
        module.fail_json(msg=f"Error fetching DNS records: {str(e)}")


def run_module():
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

    result = dict(
        changed=False,
        records=[],
        zone_id='',
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True,
        required_one_of=[
            ['auth_token', 'auth_key']
        ],
        required_together=[
            ['auth_email', 'auth_key']
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
        zone_name = module.params['zone_name']
        record = module.params['record']

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


def main():
    """
    Module entry point.
    """
    run_module()


if __name__ == '__main__':
    main()
