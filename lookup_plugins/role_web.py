from __future__ import annotations

DOCUMENTATION = """
    name: role_web
    description:
      - Resolves a role's web subdomain and domain through C(role_var).
      - Returns the hostname when C(scheme) is omitted.
      - Returns C(scheme://hostname) when C(scheme) is provided.
      - An empty subdomain resolves to the bare domain.
    author: salty
    options:
      _terms:
        description: Positional terms are not supported.
        required: false
      role:
        description: Role name whose web variables should be resolved.
        type: str
        required: true
      endpoint:
        description:
          - Web variable family without the leading underscore.
          - For example, C(web_jf) resolves C(_web_jf_subdomain) and C(_web_jf_domain).
        type: str
        required: false
        default: web
      scheme:
        description:
          - URL scheme to prepend.
          - When omitted, the lookup returns only the hostname.
        type: str
        required: false
        choices:
          - http
          - https
"""

EXAMPLES = """
- name: Resolve a canonical HTTPS URL
  ansible.builtin.debug:
    msg: "{{ lookup('role_web', role='sonarr', scheme='https') }}"

- name: Resolve a hostname
  ansible.builtin.debug:
    msg: "{{ lookup('role_web', role='authentik') }}"

- name: Resolve a named endpoint
  ansible.builtin.debug:
    msg: "{{ lookup('role_web', role='silo', endpoint='web_jf') }}"
"""

import re
from typing import Any

from ansible.errors import AnsibleLookupError
from ansible.plugins.loader import lookup_loader
from ansible.plugins.lookup import LookupBase

_ENDPOINT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SCHEMES = frozenset({"http", "https"})


class LookupModule(LookupBase):
    def _resolve_role_var(
        self,
        suffix: str,
        role: str,
        variables: dict[str, Any],
    ) -> Any:
        """Resolve one suffix through the existing role_var interface."""
        role_var = lookup_loader.get(
            "role_var",
            loader=self._loader,
            templar=self._templar,
        )
        if role_var is None:
            raise AnsibleLookupError("[role_web] Unable to load role_var")

        values = role_var.run([suffix], variables=variables, role=role)
        if len(values) != 1:
            raise AnsibleLookupError(
                f"[role_web] role_var returned {len(values)} values for "
                f"role='{role}', suffix='{suffix}'"
            )
        return values[0]

    def run(  # type: ignore[override]
        self,
        terms: list[Any],
        variables: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Resolve one role web hostname or URL."""
        if variables is None:
            variables = {}
        if terms:
            raise AnsibleLookupError(
                "[role_web] Positional terms are not supported; use role=, "
                "endpoint=, and scheme="
            )

        self.set_options(var_options=variables, direct=kwargs)
        role: Any = self.get_option("role")
        endpoint: Any = self.get_option("endpoint")
        scheme: Any = self.get_option("scheme")

        if self._templar is None:
            raise AnsibleLookupError("[role_web] Templar is not initialized")

        role = self._templar.template(role, fail_on_undefined=True)
        endpoint = self._templar.template(endpoint, fail_on_undefined=True)
        if scheme is not None:
            scheme = self._templar.template(scheme, fail_on_undefined=True)

        if not isinstance(role, str) or not role:
            raise AnsibleLookupError("[role_web] 'role' must be a non-empty string")
        if (
            not isinstance(endpoint, str)
            or _ENDPOINT_PATTERN.fullmatch(endpoint) is None
        ):
            raise AnsibleLookupError("[role_web] 'endpoint' must match [a-z][a-z0-9_]*")
        if scheme is not None and scheme not in _SCHEMES:
            raise AnsibleLookupError(
                "[role_web] 'scheme' must be either 'http' or 'https'"
            )

        prefix = f"_{endpoint}"
        subdomain_suffix = f"{prefix}_subdomain"
        domain_suffix = f"{prefix}_domain"
        subdomain = self._resolve_role_var(subdomain_suffix, role, variables)
        domain = self._resolve_role_var(domain_suffix, role, variables)
        omit_token = variables.get("omit")

        if omit_token is not None and (subdomain is omit_token or domain is omit_token):
            return [omit_token]
        if not isinstance(subdomain, str):
            raise AnsibleLookupError(
                f"[role_web] role='{role}', endpoint='{endpoint}': "
                f"{subdomain_suffix} must resolve to a string"
            )
        if not isinstance(domain, str):
            raise AnsibleLookupError(
                f"[role_web] role='{role}', endpoint='{endpoint}': "
                f"{domain_suffix} must resolve to a string"
            )

        host = f"{subdomain}.{domain}" if len(subdomain) > 0 else domain
        return [f"{scheme}://{host}" if scheme is not None else host]
