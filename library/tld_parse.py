#!/usr/bin/python
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false

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

from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule  # type: ignore[import-untyped]
from tld import get_tld

if TYPE_CHECKING:
    from tld.utils import Result


class ParsedDomain(TypedDict):
    fld: str
    subdomain: str
    record: str
    tld: str
    domain: str


class DomainParseError(ValueError):
    """Raised when a domain value cannot be normalized or parsed."""


def normalize_hostname(value: object) -> str:
    if not isinstance(value, str):
        raise DomainParseError("domain must be a string")

    normalized = value.strip()
    if not normalized:
        raise DomainParseError("domain must not be empty")

    parsed_url = urlsplit(normalized if "://" in normalized else f"//{normalized}")
    hostname = parsed_url.hostname
    if not hostname:
        raise DomainParseError(f"could not extract a hostname from {value!r}")

    hostname = hostname.rstrip(".")
    if not hostname:
        raise DomainParseError(f"could not extract a hostname from {value!r}")
    return hostname


def parse_domain(value: object, record: object = "") -> ParsedDomain:
    hostname = normalize_hostname(value)
    if not isinstance(record, str):
        raise DomainParseError("record must be a string")

    normalized_record = record.strip().rstrip(".")
    if normalized_record:
        hostname = f"{normalized_record}.{hostname}"

    try:
        result = cast("Result", get_tld(f"http://{hostname}", as_object=True))
    except Exception as exc:
        raise DomainParseError(f"failed to parse domain {value!r}: {exc}") from exc

    subdomain = result.subdomain or ""
    return {
        "fld": result.fld,
        "subdomain": subdomain,
        "record": subdomain or "@",
        "tld": result.tld,
        "domain": result.domain,
    }


def main() -> None:
    module = AnsibleModule(
        argument_spec={
            "url": {"type": "str", "required": True},
            "record": {"type": "str", "default": ""},
        },
        supports_check_mode=True,
    )
    try:
        url = cast(object, module.params["url"])
        record = cast(object, module.params["record"])
        result = parse_domain(
            url,
            record,
        )
    except DomainParseError as exc:
        module.fail_json(msg=str(exc))
    module.exit_json(changed=False, **result)


if __name__ == "__main__":
    main()
