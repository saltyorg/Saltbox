from __future__ import annotations

# pyright: reportMissingTypeStubs=false
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlsplit

from ansible.errors import AnsibleFilterError  # type: ignore[import-untyped]
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


class CertificateDomain(TypedDict):
    main: str
    sans: list[str]


def _normalize_hostname(value: object) -> str:
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


def _parse_domain(value: object, record: object = "") -> ParsedDomain:
    hostname = _normalize_hostname(value)
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


def _normalize_certificate_name(value: object, description: str) -> str:
    if not isinstance(value, str):
        raise DomainParseError(f"{description} must be a string")

    normalized = value.strip().rstrip(".")
    if (
        not normalized
        or "://" in normalized
        or "/" in normalized
        or normalized.startswith("*.")
    ):
        raise DomainParseError(
            f"{description} must be a domain without a URL or wildcard"
        )

    return _normalize_hostname(normalized)


def _wildcard_parent(value: object) -> str:
    parsed = _parse_domain(_normalize_certificate_name(value, "FQDN override"))
    record_labels = parsed["subdomain"].split(".") if parsed["subdomain"] else []
    return ".".join([*record_labels[1:], parsed["fld"]])


def tld_parse(value: object, record: object = "") -> ParsedDomain:
    """Expose Saltbox domain parsing to Jinja expressions."""
    try:
        return _parse_domain(value, record)
    except DomainParseError as exc:
        raise AnsibleFilterError(str(exc)) from exc


def _as_string_list(values: object, description: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise AnsibleFilterError(f"{description} must be a list of strings")
    if not all(isinstance(value, str) for value in values):
        raise AnsibleFilterError(f"{description} must be a list of strings")
    return [cast(str, value) for value in values]


def _belongs_to_zone(domain: str, zone: str) -> bool:
    return domain == zone or domain.endswith(f".{zone}")


def _validate_accessible_zone(domain: str, authoritative_zones: Sequence[str]) -> None:
    if any(_belongs_to_zone(domain, zone) for zone in authoritative_zones):
        return
    raise AnsibleFilterError(
        f"certificate domain {domain!r} does not belong to a zone accessible "
        + "to the configured Cloudflare credentials"
    )


def traefik_certificate_domains(
    primary_domain: object,
    fqdn_overrides: object = None,
    additional_domains: object = None,
    wildcard_enabled: bool = True,
    authoritative_zones: object = None,
) -> list[CertificateDomain]:
    """Build ordered root-and-wildcard domain objects for a Traefik router."""
    if not wildcard_enabled:
        return []

    fqdn_values = _as_string_list(fqdn_overrides, "FQDN overrides")
    additional_values = _as_string_list(additional_domains, "additional domains")
    zone_values: list[str] | None
    if authoritative_zones is None:
        zone_values = None
    else:
        zone_values = _as_string_list(authoritative_zones, "authoritative zones")
        if not zone_values:
            raise AnsibleFilterError(
                "the configured Cloudflare credentials returned no zones"
            )
        zone_values = [zone.rstrip(".").casefold() for zone in zone_values]

    try:
        primary = _normalize_certificate_name(primary_domain, "primary domain")
        _ = _parse_domain(primary)

        explicit_bases = [
            _normalize_certificate_name(domain, "additional domain")
            for domain in additional_values
        ]
        for domain in explicit_bases:
            _ = _parse_domain(domain)

        inferred_bases = [_wildcard_parent(fqdn) for fqdn in fqdn_values]
    except DomainParseError as exc:
        raise AnsibleFilterError(str(exc)) from exc

    unique_bases: list[str] = []
    seen: set[str] = set()
    for base in [primary, *explicit_bases, *inferred_bases]:
        comparison_key = base.casefold()
        if comparison_key in seen:
            continue
        if zone_values is not None:
            _validate_accessible_zone(comparison_key, zone_values)
        seen.add(comparison_key)
        unique_bases.append(base)

    return [{"main": base, "sans": [f"*.{base}"]} for base in unique_bases]


def traefik_certificate_labels(domains: object, router: object) -> dict[str, str]:
    """Encode structured certificate domains as indexed Docker labels."""
    if not isinstance(router, str) or not router:
        raise AnsibleFilterError("Traefik router name must not be empty")
    if isinstance(domains, (str, bytes)) or not isinstance(domains, Sequence):
        raise AnsibleFilterError("certificate domains must be a list")

    labels: dict[str, str] = {}
    for index, domain in enumerate(domains):
        if not isinstance(domain, dict):
            raise AnsibleFilterError("certificate domains must contain mappings")
        domain_mapping = cast(dict[object, object], domain)
        main = domain_mapping.get("main")
        sans = domain_mapping.get("sans")
        if not isinstance(main, str) or not main:
            raise AnsibleFilterError(
                "certificate domain main values must be non-empty strings"
            )
        sans_values = _as_string_list(sans, "certificate domain SANs")
        prefix = f"traefik.http.routers.{router}.tls.domains[{index}]"
        labels[f"{prefix}.main"] = main
        labels[f"{prefix}.sans"] = ",".join(sans_values)

    return labels


class FilterModule:
    """Register Traefik certificate filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        return {
            "tld_parse": tld_parse,
            "traefik_certificate_domains": traefik_certificate_domains,
            "traefik_certificate_labels": traefik_certificate_labels,
        }
