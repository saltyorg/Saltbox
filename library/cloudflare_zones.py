#!/usr/bin/python
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

from __future__ import annotations

DOCUMENTATION = """
---
module: cloudflare_zones
author: salty
short_description: Return zones accessible to Cloudflare credentials
description:
  - Fetches every Cloudflare zone accessible to the supplied credentials.
  - This module is read-only and supports check mode.
requirements:
  - cloudflare==5.6.0
options:
  auth_email:
    description: Cloudflare account email.
    required: true
    type: str
  auth_key:
    description: Cloudflare global API key.
    required: true
    type: str
"""

EXAMPLES = """
- name: Fetch accessible Cloudflare zones
  cloudflare_zones:
    auth_email: "{{ cloudflare.email }}"
    auth_key: "{{ cloudflare.api }}"
  register: cloudflare_zone_catalog
"""

RETURN = """
zones:
  description: Normalized names of every accessible Cloudflare zone.
  type: list
  elements: str
  returned: always
  sample:
    - example.com
changed:
  description: Whether any state changed; always false.
  type: bool
  returned: always
"""

from typing import TYPE_CHECKING, cast

from ansible.module_utils.basic import AnsibleModule  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from cloudflare import Cloudflare


def fetch_zone_names(client: Cloudflare) -> list[str]:
    response = client.zones.list(per_page=50)
    zone_names: list[str] = []
    for page in response.iter_pages():
        zone_names.extend(zone.name.rstrip(".").casefold() for zone in page.result)
    return list(dict.fromkeys(zone_names))


def main() -> None:
    module = AnsibleModule(
        argument_spec={
            "auth_email": {
                "type": "str",
                "required": True,
                "no_log": False,
            },
            "auth_key": {
                "type": "str",
                "required": True,
                "no_log": True,
            },
        },
        supports_check_mode=True,
    )

    try:
        from cloudflare import Cloudflare, CloudflareError
    except ImportError:
        module.fail_json(msg="the 'cloudflare' Python library is required")

    try:
        auth_email = cast(str, module.params["auth_email"])
        auth_key = cast(str, module.params["auth_key"])
        client = Cloudflare(
            api_email=auth_email,
            api_key=auth_key,
        )
        zones = fetch_zone_names(client)
    except CloudflareError as exc:
        module.fail_json(
            msg=(f"unable to fetch Cloudflare zones ({type(exc).__name__})")
        )

    module.exit_json(changed=False, zones=zones)


if __name__ == "__main__":
    main()
