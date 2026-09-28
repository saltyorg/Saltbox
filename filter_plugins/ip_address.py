from __future__ import annotations

from collections.abc import Callable
from ipaddress import ip_address

from ansible.errors import AnsibleFilterError


def saltbox_ip_address(value: object, family: int) -> str:
    """Validate and normalize an address for a DNS record or port binding."""
    if not isinstance(value, str) or not value.strip() or "%" in value:
        raise AnsibleFilterError(f"Expected an IPv{family} address, got {value!r}")
    try:
        address = ip_address(value.strip())
    except ValueError as exc:
        raise AnsibleFilterError(f"Expected an IPv{family} address, got {value!r}") from exc
    if address.version != family:
        raise AnsibleFilterError(f"Expected an IPv{family} address, got {value!r}")
    return str(address)


class FilterModule:
    def filters(self) -> dict[str, Callable[..., str]]:
        return {"saltbox_ip_address": saltbox_ip_address}
