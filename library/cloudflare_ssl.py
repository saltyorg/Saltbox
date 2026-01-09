#!/usr/bin/python
"""
Ansible module for retrieving Cloudflare SSL/TLS encryption mode.

This module provides functionality to get the SSL/TLS encryption mode
for a specific Cloudflare zone. It can automatically parse domain names
to extract the zone using the tld library.

Example Usage:
    # Get SSL/TLS mode for a zone (automatically parses domain)
    - name: Get Cloudflare SSL mode
      cloudflare_ssl:
        auth_email: "user@example.com"
        auth_key: "api_key_here"
        domain: "subdomain.example.com"
      register: ssl_mode

    # Get SSL/TLS mode with explicit zone name
    - name: Get Cloudflare SSL mode with zone
      cloudflare_ssl:
        auth_token: "token_here"
        zone_name: "example.com"
      register: ssl_mode

Return Values:
    ssl_mode:
        description: The SSL/TLS encryption mode for the zone
        type: str
        returned: success
        sample: 'full'
    zone_id:
        description: The Cloudflare zone ID for the specified zone
        type: str
        returned: success
    zone_name:
        description: The zone name that was queried
        type: str
        returned: success
    changed:
        description: Whether any changes were made (always False for this read-only module)
        type: bool
        returned: always
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ansible.module_utils.basic import AnsibleModule

if TYPE_CHECKING:
    from cloudflare import Cloudflare

DOCUMENTATION = """
---
module: cloudflare_ssl
short_description: Retrieve Cloudflare SSL/TLS encryption mode for a zone
description:
    - "This module retrieves the SSL/TLS encryption mode for a specific Cloudflare zone."
    - "It supports both API key and API token authentication methods."
    - "Can automatically extract the zone from a domain using the tld library."
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
    domain:
        description:
            - Domain name to parse and extract zone from (e.g., subdomain.example.com)
            - Mutually exclusive with zone_name
        required: false
        type: str
    zone_name:
        description:
            - Name of the Cloudflare zone (e.g., example.com)
            - Mutually exclusive with domain
        required: false
        type: str
"""

EXAMPLES = """
# Get SSL/TLS mode using domain (automatically extracts zone)
- name: Get Cloudflare SSL mode with domain
  cloudflare_ssl:
    auth_email: "user@example.com"
    auth_key: "{{ cloudflare_api_key }}"
    domain: "{{ user.domain }}"
  register: ssl_mode

# Get SSL/TLS mode using explicit zone name
- name: Get Cloudflare SSL mode with zone
  cloudflare_ssl:
    auth_email: "user@example.com"
    auth_key: "{{ cloudflare_api_key }}"
    zone_name: "example.com"
  register: ssl_mode

# Display the SSL mode
- name: Display SSL mode
  debug:
    msg: "SSL/TLS mode: {{ ssl_mode.ssl_mode }}"
"""


def get_zone_id(client: Cloudflare, zone_name: str, module: AnsibleModule) -> str:
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
        raise # Unreachable - Pylance silencer


def get_ssl_tls_mode(client: Cloudflare, zone_id: str, module: AnsibleModule) -> str:
    """
    Get the SSL/TLS settings for a zone.

    Args:
        client: Cloudflare client instance
        zone_id (str): The Cloudflare zone ID
        module: AnsibleModule instance for error reporting

    Returns:
        str: The SSL/TLS mode value

    Raises:
        Calls module.fail_json on error
    """
    try:
        ssl_response = client.zones.settings.get(setting_id='ssl', zone_id=zone_id)
        if ssl_response is None:
            module.fail_json(msg="No response from Cloudflare API")

        ssl_settings = ssl_response.to_dict()
        ssl_mode = ssl_settings.get('value')

        if ssl_mode is None:
            module.fail_json(msg="SSL/TLS mode value not found in API response")

        return str(ssl_mode)
    except Exception as e:
        module.fail_json(msg=f"Error fetching SSL/TLS settings: {str(e)}")
        raise # Unreachable - Pylance silencer


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
        domain=dict(type='str', required=False),
        zone_name=dict(type='str', required=False),
    )

    result = dict(
        changed=False,
        ssl_mode='',
        zone_id='',
        zone_name='',
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True,
        required_one_of=[
            ['auth_token', 'auth_key'],
            ['domain', 'zone_name']
        ],
        required_together=[
            ['auth_email', 'auth_key']
        ],
        mutually_exclusive=[
            ['auth_token', 'auth_key'],
            ['domain', 'zone_name']
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
        domain = module.params.get('domain')
        zone_name = module.params.get('zone_name')

        # Parse domain to get zone name if domain is provided
        if domain:
            try:
                from tld import get_tld
                res = get_tld(f"http://{domain}", as_object=True)
                zone_name = getattr(res, 'fld', None)
                if not zone_name:
                    module.fail_json(msg=f"Failed to extract zone name from domain '{domain}'")
            except ImportError:
                module.fail_json(msg="The 'tld' Python library is required for domain parsing. Install it with: pip install tld")
            except Exception as e:
                module.fail_json(msg=f"Failed to parse domain '{domain}': {str(e)}")

        # Ensure zone_name is set
        if not zone_name:
            module.fail_json(msg="Zone name could not be determined from provided parameters")

        # Initialize Cloudflare client
        if auth_token:
            cf = Cloudflare(api_token=auth_token)
        else:
            cf = Cloudflare(api_email=auth_email, api_key=auth_key)

        # Fetch zone ID
        zone_id = get_zone_id(cf, zone_name, module)
        result['zone_id'] = zone_id
        result['zone_name'] = zone_name

        # Fetch SSL/TLS mode
        ssl_mode = get_ssl_tls_mode(cf, zone_id, module)
        result['ssl_mode'] = ssl_mode

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
