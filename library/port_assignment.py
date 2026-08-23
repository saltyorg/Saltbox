# -*- coding: utf-8 -*-

from __future__ import annotations

DOCUMENTATION = r"""
---
module: port_assignment
short_description: Persist stable host port assignments
description:
  - Reconciles named host port claims against a persistent Saltbox registry.
  - Treats listening sockets and Docker bindings from running or stopped containers as conflicts.
  - Automatically moves a saved assignment when it conflicts and warns about the change.
author: salty
options:
  base_path:
    description:
      - Base directory under which the Saltbox port registry is stored.
    required: true
    type: path
  namespace:
    description:
      - Logical role or workload group containing the owner.
    required: true
    type: str
  owner:
    description:
      - Stable workload or instance identity.
      - Required when C(state=present) and optional when C(state=absent).
    required: false
    type: str
  state:
    description:
      - Reconcile the owner's complete claim set or release saved claims.
      - C(absent) releases one owner when C(owner) is provided, otherwise the complete namespace.
    choices: [present, absent]
    default: present
    type: str
  claims:
    description:
      - Complete mapping of logical claim names to allocation constraints.
      - Each claim requires C(low_bound), C(high_bound), and a C(protocols) list.
      - C(enabled) is optional, defaults to C(true), and releases a saved claim when C(false).
      - Claims previously saved for the owner but omitted from this mapping are released.
    required: false
    default: {}
    type: dict
requirements:
  - iproute2
  - Docker SDK for Python when Docker is available
notes:
  - The caller must stop its service or remove its container before allocation.
  - Allocation uses inclusive bounds when a claim has no saved port or its saved port conflicts.
  - A conflict-free saved port remains authoritative even when it is outside the current bounds.
  - A saved-port conflict reassigns within the current bounds and warns.
  - Check mode is not supported.
  - The persistent lock file coordinates Saltbox callers but cannot reserve a port after the module exits.
"""

EXAMPLES = r"""
- name: Assign a qBittorrent peer port
  port_assignment:
    base_path: "{{ server_appdata_path }}"
    namespace: qbittorrent
    owner: "{{ qbittorrent_name }}"
    claims:
      peer:
        low_bound: 56881
        high_bound: 56901
        protocols: ["tcp", "udp"]
  register: qbittorrent_port_assignment

- name: Release all assignments for a removed role
  port_assignment:
    base_path: "{{ server_appdata_path }}"
    namespace: removed_role
    state: absent
"""

RETURN = r"""
ports:
  description: Effective ports keyed by logical claim name.
  returned: when state is present
  type: dict
released_claims:
  description: Registry claim keys released by this invocation.
  returned: when state is absent
  type: list
"""

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable
from typing import Any

from ansible.module_utils.basic import AnsibleModule

SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 30.0

ConflictIndex = dict[tuple[int, str], list[str]]
ConflictCollector = Callable[[], ConflictIndex]


class PortAssignmentError(ValueError):
    pass


def _validate_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortAssignmentError(f"{field} must be a non-empty string")
    if "/" in value or any(character in value for character in ("\r", "\n")):
        raise PortAssignmentError(
            f"{field} must not contain '/', carriage returns, or newlines"
        )
    return value


def _validate_port(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise PortAssignmentError(f"{field} must be an integer between 1 and 65535")
    return value


def _normalize_request(request: dict[str, Any]) -> None:
    claims = request.get("claims")
    if not isinstance(claims, dict):
        return
    for claim in claims.values():
        if not isinstance(claim, dict):
            continue
        for field in ("low_bound", "high_bound"):
            value = claim.get(field)
            if not isinstance(value, str):
                continue
            stripped = value.strip()
            if stripped.isdigit():
                claim[field] = int(stripped)


def _validate_request(request: dict[str, Any]) -> None:
    unsupported_fields = sorted(
        set(request) - {"namespace", "owner", "state", "claims"}
    )
    if unsupported_fields:
        raise PortAssignmentError(
            f"Request contains unsupported field(s): {', '.join(unsupported_fields)}"
        )

    _validate_identifier(request.get("namespace"), "namespace")
    state = request.get("state", "present")
    if state not in ("present", "absent"):
        raise PortAssignmentError("state must be 'present' or 'absent'")

    claims = request.get("claims", {})
    if not isinstance(claims, dict):
        raise PortAssignmentError("claims must be a mapping")

    owner = request.get("owner")
    if state == "absent":
        if owner is not None:
            _validate_identifier(owner, "owner")
        if claims:
            raise PortAssignmentError("claims must be empty when state is 'absent'")
        return

    _validate_identifier(owner, "owner")
    if not claims:
        raise PortAssignmentError("claims must not be empty when state is 'present'")

    allowed_claim_fields = {"enabled", "low_bound", "high_bound", "protocols"}
    for claim_name, claim in claims.items():
        _validate_identifier(claim_name, "claim name")
        if not isinstance(claim, dict):
            raise PortAssignmentError(f"Claim '{claim_name}' must be a mapping")
        unsupported_claim_fields = sorted(set(claim) - allowed_claim_fields)
        if unsupported_claim_fields:
            raise PortAssignmentError(
                f"Claim '{claim_name}' contains unsupported field(s): "
                f"{', '.join(unsupported_claim_fields)}"
            )

        enabled = claim.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PortAssignmentError(f"Claim '{claim_name}' enabled must be a boolean")

        low_bound = _validate_port(
            claim.get("low_bound"), f"Claim '{claim_name}' low_bound"
        )
        high_bound = _validate_port(
            claim.get("high_bound"), f"Claim '{claim_name}' high_bound"
        )
        if high_bound < low_bound:
            raise PortAssignmentError(
                f"Claim '{claim_name}' low_bound must be less than or equal to high_bound"
            )

        protocols = claim.get("protocols")
        if not isinstance(protocols, list) or not protocols:
            raise PortAssignmentError(
                f"Claim '{claim_name}' protocols must be a non-empty list"
            )
        invalid_protocols = sorted(set(protocols) - {"tcp", "udp"})
        if invalid_protocols:
            raise PortAssignmentError(
                f"Claim '{claim_name}' protocols contain unsupported values: "
                f"{', '.join(invalid_protocols)}"
            )


def _validate_registry(registry: dict[str, Any]) -> None:
    if set(registry) != {"schema", "claims"}:
        raise PortAssignmentError("registry must contain only 'schema' and 'claims'")
    if registry.get("schema") != SCHEMA_VERSION or not isinstance(
        registry.get("claims"), dict
    ):
        raise PortAssignmentError("registry has an unsupported schema")

    allocated: dict[tuple[int, str], str] = {}
    for key, claim in registry["claims"].items():
        if not isinstance(key, str) or len(key.split("/")) != 3:
            raise PortAssignmentError(f"Invalid registry claim key: {key!r}")
        for index, identifier in enumerate(key.split("/")):
            _validate_identifier(
                identifier, f"registry claim '{key}' key part {index + 1}"
            )
        if not isinstance(claim, dict):
            raise PortAssignmentError(
                f"Invalid registry claim '{key}': expected a mapping"
            )
        if set(claim) != {"port", "protocols", "range"}:
            raise PortAssignmentError(
                f"Invalid registry claim '{key}': expected only port, protocols, and range"
            )

        try:
            port = _validate_port(claim.get("port"), f"registry claim '{key}' port")
        except PortAssignmentError as error:
            raise PortAssignmentError(
                f"Invalid registry claim '{key}' port {claim.get('port')!r}: {error}"
            ) from error
        protocols = claim.get("protocols")
        if (
            not isinstance(protocols, list)
            or not protocols
            or set(protocols) - {"tcp", "udp"}
        ):
            raise PortAssignmentError(f"Invalid registry claim '{key}' protocols")

        range_value = claim.get("range")
        if not isinstance(range_value, dict) or set(range_value) != {"low", "high"}:
            raise PortAssignmentError(f"Invalid registry claim '{key}' range")
        low_bound = _validate_port(
            range_value.get("low"), f"registry claim '{key}' range low"
        )
        high_bound = _validate_port(
            range_value.get("high"), f"registry claim '{key}' range high"
        )
        if high_bound < low_bound:
            raise PortAssignmentError(f"Invalid registry claim '{key}' range ordering")

        for protocol in protocols:
            allocation = (port, protocol)
            if allocation in allocated:
                raise PortAssignmentError(
                    f"Conflicting registry claims '{allocated[allocation]}' and '{key}' "
                    f"both allocate {port}/{protocol}"
                )
            allocated[allocation] = key


def _registry_path(base_path: str) -> str:
    if not os.path.isabs(base_path):
        raise PortAssignmentError("base_path must be absolute")
    return os.path.join(os.path.normpath(base_path), "saltbox", "port-assignments.json")


def _read_registry(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"schema": SCHEMA_VERSION, "claims": {}}
    if os.path.islink(path):
        raise PortAssignmentError(f"Registry path must not be a symbolic link: {path}")
    try:
        with open(path, "r", encoding="utf-8") as registry_file:
            registry = json.load(registry_file)
    except (OSError, json.JSONDecodeError) as error:
        raise PortAssignmentError(
            f"Unable to read port registry '{path}': {error}"
        ) from error
    try:
        _validate_registry(registry)
    except PortAssignmentError as error:
        raise PortAssignmentError(f"Invalid port registry '{path}': {error}") from error
    return registry


def _atomic_write_registry(path: str, registry: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=".port-assignments-", dir=directory
    )
    try:
        with os.fdopen(
            file_descriptor, "w", encoding="utf-8", newline="\n"
        ) as registry_file:
            json.dump(registry, registry_file, indent=2, sort_keys=True)
            registry_file.write("\n")
            registry_file.flush()
            os.fsync(registry_file.fileno())
            os.fchmod(registry_file.fileno(), 0o644)
        os.replace(temporary_path, path)
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if os.path.lexists(temporary_path):
            os.unlink(temporary_path)
        raise


def _acquire_registry_lock(lock_file: Any, lock_path: str, lock_timeout: float) -> None:
    deadline = time.monotonic() + lock_timeout
    while True:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PortAssignmentError(
                    f"Timed out after {lock_timeout:g} seconds waiting for port registry lock "
                    f"'{lock_path}'"
                )
            time.sleep(min(0.05, remaining))


def _add_conflict(
    conflicts: ConflictIndex,
    port: int,
    protocol: str,
    source: str,
) -> None:
    sources = conflicts.setdefault((port, protocol), [])
    if source not in sources:
        sources.append(source)
        sources.sort()


def parse_socket_observations(output: str) -> ConflictIndex:
    conflicts: ConflictIndex = {}
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 5:
            raise PortAssignmentError(f"Unexpected ss output: {line}")
        protocol = fields[0]
        if protocol not in ("tcp", "udp"):
            continue
        if protocol == "tcp" and fields[1] != "LISTEN":
            continue
        port_text = fields[4].rsplit(":", 1)[-1]
        if not port_text.isdigit():
            raise PortAssignmentError(f"Could not parse port from ss output: {line}")
        port = _validate_port(int(port_text), "ss port")
        _add_conflict(
            conflicts,
            port,
            protocol,
            "listening TCP socket" if protocol == "tcp" else "bound UDP socket",
        )
    return conflicts


def parse_docker_observations(containers: list[dict[str, Any]]) -> ConflictIndex:
    conflicts: ConflictIndex = {}
    for container in containers:
        name = str(container.get("Name", "")).lstrip("/") or "unknown"
        status = str(container.get("State", {}).get("Status", "unknown"))
        port_bindings = container.get("HostConfig", {}).get("PortBindings") or {}
        for container_binding, host_bindings in port_bindings.items():
            try:
                _, protocol = container_binding.rsplit("/", 1)
            except ValueError:
                continue
            if protocol not in ("tcp", "udp") or not host_bindings:
                continue
            for host_binding in host_bindings:
                if not isinstance(host_binding, dict):
                    continue
                port_text = str(host_binding.get("HostPort", ""))
                if not port_text.isdigit() or int(port_text) == 0:
                    continue
                _add_conflict(
                    conflicts,
                    _validate_port(int(port_text), "Docker host port"),
                    protocol,
                    f"container {name} ({status})",
                )
    return conflicts


def merge_conflicts(
    socket_conflicts: ConflictIndex,
    docker_conflicts: ConflictIndex,
) -> ConflictIndex:
    merged = {
        binding: list(sources)
        for binding, sources in socket_conflicts.items()
        if binding not in docker_conflicts
    }
    for binding, sources in docker_conflicts.items():
        merged[binding] = list(sources)
    return merged


def collect_socket_observations(module: AnsibleModule) -> ConflictIndex:
    ss_path = module.get_bin_path("ss", required=True)
    return_code, standard_output, standard_error = module.run_command(
        [ss_path, "-Htuan"]
    )
    if return_code != 0:
        detail = standard_error.strip() or standard_output.strip()
        raise PortAssignmentError(f"Failed to execute ss command: {detail}")
    return parse_socket_observations(standard_output)


def collect_docker_observations() -> ConflictIndex:
    docker_socket = "/var/run/docker.sock"
    if not os.path.exists(docker_socket):
        return {}
    try:
        import docker  # type: ignore[import-not-found]
    except ImportError as error:
        raise PortAssignmentError(
            "Docker SDK for Python is required to inspect published port declarations"
        ) from error
    try:
        client = docker.from_env()
        try:
            containers = [
                container.attrs for container in client.containers.list(all=True)
            ]
        finally:
            client.close()
    except Exception as error:
        raise PortAssignmentError(
            f"Unable to inspect Docker containers: {error}"
        ) from error
    return parse_docker_observations(containers)


def collect_conflicts(module: AnsibleModule) -> ConflictIndex:
    return merge_conflicts(
        collect_socket_observations(module),
        collect_docker_observations(),
    )


def _claim_key(namespace: str, owner: str, claim: str) -> str:
    return f"{namespace}/{owner}/{claim}"


def _port_conflict_sources(
    port: int,
    protocols: list[str],
    ignored_claim_key: str,
    registry_claims: dict[str, dict[str, Any]],
    observations: ConflictIndex,
) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for key, existing in registry_claims.items():
        if key == ignored_claim_key or existing["port"] != port:
            continue
        for protocol in protocols:
            if protocol in existing["protocols"]:
                sources.append((protocol, f"registered claim {key}"))
    for protocol in protocols:
        for source in observations.get((port, protocol), []):
            sources.append((protocol, source))
    return sorted(set(sources))


def _format_conflicts(port: int, conflicts: list[tuple[str, str]]) -> str:
    return ", ".join(f"{port}/{protocol} {source}" for protocol, source in conflicts)


def reconcile_assignments(
    registry: dict[str, Any],
    request: dict[str, Any],
    observations: ConflictIndex,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _normalize_request(request)
    _validate_request(request)
    _validate_registry(registry)

    namespace = request["namespace"]
    owner = request.get("owner")
    state = request.get("state", "present")
    updated_registry = {
        "schema": SCHEMA_VERSION,
        "claims": dict(registry["claims"]),
    }
    released_claims: list[str] = []
    ports: dict[str, int] = {}
    warnings: list[str] = []

    if state == "absent":
        prefix = f"{namespace}/{owner}/" if owner is not None else f"{namespace}/"
        for key in list(updated_registry["claims"]):
            if key.startswith(prefix):
                released_claims.append(key)
                del updated_registry["claims"][key]
        return updated_registry, {
            "ports": ports,
            "released_claims": sorted(released_claims),
            "warnings": warnings,
        }

    claims = {
        claim_name: claim
        for claim_name, claim in request["claims"].items()
        if claim.get("enabled", True)
    }
    owner_prefix = f"{namespace}/{owner}/"
    desired_keys = {_claim_key(namespace, owner, claim_name) for claim_name in claims}
    for key in list(updated_registry["claims"]):
        if key.startswith(owner_prefix) and key not in desired_keys:
            released_claims.append(key)
            del updated_registry["claims"][key]

    for claim_name, claim in claims.items():
        key = _claim_key(namespace, owner, claim_name)
        existing_claim = updated_registry["claims"].get(key)
        protocols = list(dict.fromkeys(claim["protocols"]))

        if existing_claim is not None:
            port = existing_claim["port"]
            conflicts = _port_conflict_sources(
                port,
                protocols,
                key,
                updated_registry["claims"],
                observations,
            )
            if conflicts:
                old_port = port
                port = None
                for candidate in range(claim["low_bound"], claim["high_bound"] + 1):
                    if not _port_conflict_sources(
                        candidate,
                        protocols,
                        key,
                        updated_registry["claims"],
                        observations,
                    ):
                        port = candidate
                        break
                if port is None:
                    raise PortAssignmentError(
                        f"No available port for claim '{claim_name}' in inclusive range "
                        f"{claim['low_bound']}-{claim['high_bound']}"
                    )
                protocol, source = conflicts[0]
                warnings.append(
                    f"Port assignment {key} moved from {old_port} to {port} because "
                    f"{old_port}/{protocol} conflicts with {source}."
                )
        else:
            port = None
            for candidate in range(claim["low_bound"], claim["high_bound"] + 1):
                if not _port_conflict_sources(
                    candidate,
                    protocols,
                    key,
                    updated_registry["claims"],
                    observations,
                ):
                    port = candidate
                    break
            if port is None:
                raise PortAssignmentError(
                    f"No available port for claim '{claim_name}' in inclusive range "
                    f"{claim['low_bound']}-{claim['high_bound']}"
                )

        updated_registry["claims"][key] = {
            "port": port,
            "protocols": protocols,
            "range": {
                "low": claim["low_bound"],
                "high": claim["high_bound"],
            },
        }
        ports[claim_name] = port

    return updated_registry, {
        "ports": ports,
        "released_claims": sorted(released_claims),
        "warnings": warnings,
    }


def apply_assignment(
    base_path: str,
    request: dict[str, Any],
    conflict_collector: ConflictCollector,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    _normalize_request(request)
    _validate_request(request)
    path = _registry_path(base_path)
    directory = os.path.dirname(path)
    if os.path.lexists(directory) and os.path.islink(directory):
        raise PortAssignmentError(
            f"Registry directory must not be a symbolic link: {directory}"
        )
    os.makedirs(directory, mode=0o755, exist_ok=True)

    lock_path = f"{path}.lock"
    if os.path.lexists(lock_path) and os.path.islink(lock_path):
        raise PortAssignmentError(
            f"Registry lock must not be a symbolic link: {lock_path}"
        )
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        with os.fdopen(lock_descriptor, "r+") as lock_file:
            _acquire_registry_lock(lock_file, lock_path, lock_timeout)
            registry = _read_registry(path)
            observations = (
                {}
                if request.get("state", "present") == "absent"
                else conflict_collector()
            )
            updated_registry, result = reconcile_assignments(
                registry, request, observations
            )
            changed = updated_registry != registry
            if changed:
                _atomic_write_registry(path, updated_registry)
            return {**result, "changed": changed}
    except OSError as error:
        raise PortAssignmentError(
            f"Unable to update port registry '{path}': {error}"
        ) from error


def main() -> None:
    module = AnsibleModule(
        argument_spec={
            "base_path": {"type": "path", "required": True},
            "namespace": {"type": "str", "required": True},
            "owner": {"type": "str"},
            "state": {
                "type": "str",
                "choices": ["present", "absent"],
                "default": "present",
            },
            "claims": {"type": "dict", "default": {}},
        },
        supports_check_mode=False,
    )
    request = {
        "namespace": module.params["namespace"],
        "owner": module.params["owner"],
        "state": module.params["state"],
        "claims": module.params["claims"],
    }
    try:
        result = apply_assignment(
            module.params["base_path"],
            request,
            lambda: collect_conflicts(module),
        )
    except PortAssignmentError as error:
        module.fail_json(msg=str(error))
    except OSError as error:
        module.fail_json(msg=f"Port assignment filesystem operation failed: {error}")

    for warning in result.pop("warnings"):
        module.warn(warning)
    if request["state"] == "absent":
        result.pop("ports")
    else:
        result.pop("released_claims")
    module.exit_json(**result)


if __name__ == "__main__":
    main()
