#!/usr/bin/python3

"""Resolve an NVIDIA driver from NVIDIA's hardware and release metadata."""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Any


DOCUMENTATION = r"""
---
module: nvidia_driver_resolver
short_description: Resolve a hardware-compatible NVIDIA driver
description:
  - Inventories NVIDIA display devices from sysfs.
  - Uses NVIDIA Driver Assistant metadata to reject unsupported hardware.
  - Selects a compatible release while applying Saltbox branch and patch policy.
options:
  catalog_path:
    description:
      - Path to the NVIDIA Driver Assistant supported GPU catalog.
    type: path
    required: true
  releases_path:
    description:
      - Path to NVIDIA's driver release metadata.
    type: path
    required: true
  patch_path:
    description:
      - Path to the pinned Keylase patch script used to identify supported releases.
    type: path
    required: false
  driver_version:
    description:
      - Driver selection mode or exact release.
      - Accepts C(latest), C(ignore), or a complete dotted release.
    type: str
    required: true
  driver_branch:
    description:
      - Driver branch selection used when O(driver_version=latest).
      - Accepts C(auto) or a numeric branch.
    type: str
    default: auto
  module_flavor:
    description:
      - NVIDIA kernel module implementation to select.
    type: str
    default: auto
  branch_preference:
    description:
      - Automatic branch policy.
    type: str
    default: lts
  patch_enabled:
    description:
      - Whether compatible GeForce releases should be constrained to the pinned Keylase patch.
    type: bool
    default: true
  minimum_legacy_branch:
    description:
      - Oldest NVIDIA legacy branch supported by Saltbox.
    type: int
    default: 580
  sysfs_path:
    description:
      - Sysfs root used for PCI hardware inventory.
    type: path
    default: /sys
  supported_products_path:
    description:
      - Optional path to a runfile supported-products document for final hardware validation.
    type: path
    required: false
author:
  - Saltbox
"""

EXAMPLES = r"""
- name: Resolve NVIDIA driver
  nvidia_driver_resolver:
    catalog_path: /usr/share/nvidia-driver-assistant/supported-gpus/supported-gpus.json
    releases_path: /var/cache/saltbox/nvidia/releases.json
    patch_path: /var/cache/saltbox/nvidia/patch.sh
    driver_version: latest
  register: nvidia_driver_resolution
"""

RETURN = r"""
devices:
  description: Detected NVIDIA devices and their compatibility metadata.
  type: list
  elements: dict
  returned: always
mode:
  description: Whether Saltbox manages the driver or validates an external driver.
  type: str
  returned: always
resolved_version:
  description: Exact driver version selected by the resolver.
  type: str
  returned: always
resolved_branch:
  description: Selected driver branch.
  type: str
  returned: always
module_flavor:
  description: Selected runfile kernel module flavor.
  type: str
  returned: always
geforce_present:
  description: Whether any detected product is a GeForce GPU.
  type: bool
  returned: always
patch_required:
  description: Whether the pinned Keylase patch should be applied.
  type: bool
  returned: always
driver_url:
  description: Official NVIDIA runfile URL.
  type: str
  returned: always
checksum_url:
  description: Official NVIDIA runfile checksum URL.
  type: str
  returned: always
"""


def version_key(version: str) -> tuple[int, ...]:
    """Return a numeric key for dotted NVIDIA versions."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version):
        raise ValueError("invalid NVIDIA driver version: %s" % version)
    return tuple(int(part) for part in version.split("."))


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read().strip()


def _normalise_pci_id(value: str) -> str:
    return "0x%s" % value.lower().removeprefix("0x").zfill(4).upper()


def inventory_devices(sysfs_path: str, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Match NVIDIA display-class sysfs devices against NVIDIA's catalog."""
    chips = catalog.get("chips")
    if not isinstance(chips, list):
        raise ValueError("NVIDIA GPU catalog does not contain a chips list")

    devices: list[dict[str, Any]] = []
    for device_path in sorted(glob.glob(os.path.join(sysfs_path, "bus", "pci", "devices", "*"))):
        try:
            vendor = _read_text(os.path.join(device_path, "vendor")).lower()
            pci_class = _read_text(os.path.join(device_path, "class")).lower()
        except OSError:
            continue
        if vendor != "0x10de" or not pci_class.startswith("0x03"):
            continue

        device_id = _normalise_pci_id(_read_text(os.path.join(device_path, "device")))
        subvendor_id = _normalise_pci_id(_read_text(os.path.join(device_path, "subsystem_vendor")))
        subdevice_id = _normalise_pci_id(_read_text(os.path.join(device_path, "subsystem_device")))
        matching = [
            chip
            for chip in chips
            if chip.get("devid") and _normalise_pci_id(str(chip["devid"])) == device_id
        ]
        exact = [
            chip
            for chip in matching
            if chip.get("subvendorid")
            and chip.get("subdevid")
            and _normalise_pci_id(str(chip["subvendorid"])) == subvendor_id
            and _normalise_pci_id(str(chip["subdevid"])) == subdevice_id
        ]
        generic = [chip for chip in matching if "subvendorid" not in chip and "subdevid" not in chip]
        selected = exact or generic
        if not selected:
            devices.append(
                {
                    "address": os.path.basename(device_path),
                    "devid": device_id,
                    "subvendorid": subvendor_id,
                    "subdevid": subdevice_id,
                    "name": "unknown",
                    "features": [],
                    "subsystem_specific": False,
                    "unknown": True,
                }
            )
            continue

        chip = selected[0]
        devices.append(
            {
                "address": os.path.basename(device_path),
                "devid": device_id,
                "subvendorid": subvendor_id,
                "subdevid": subdevice_id,
                "name": chip.get("name", "unknown"),
                "features": chip.get("features", []),
                "legacybranch": chip.get("legacybranch"),
                "subsystem_specific": bool(exact),
                "unknown": False,
            }
        )
    return devices


def patch_versions(patch_content: str) -> set[str]:
    """Extract exact versions from Keylase's associative patch table."""
    return set(re.findall(r'^\s*\["([0-9.]+)"\]=', patch_content, flags=re.MULTILINE))


def validate_supported_products(devices: list[dict[str, Any]], content: str) -> None:
    """Require every PCI ID in the runfile's current-product section."""
    current_products = content.split("Below are the legacy GPUs", maxsplit=1)[0].upper()
    unsupported = []
    for device in devices:
        device_id = str(device["devid"]).removeprefix("0x").upper()
        generic_marker = 'ID="DEVID%s"' % device_id
        supported_markers = [generic_marker]
        if device.get("subsystem_specific"):
            subvendor_id = str(device["subvendorid"]).removeprefix("0x").upper()
            subdevice_id = str(device["subdevid"]).removeprefix("0x").upper()
            supported_markers.append(
                'ID="DEVID%s_%s_%s"' % (device_id, subvendor_id, subdevice_id)
            )
        if not any(marker in current_products for marker in supported_markers):
            unsupported.append(device)
    if unsupported:
        details = ", ".join(
            "%s (%s at %s)" % (device["name"], device["devid"], device["address"])
            for device in unsupported
        )
        raise ValueError("the selected NVIDIA runfile does not support: %s" % details)


def _module_flavor(devices: list[dict[str, Any]], requested: str) -> str:
    features = [{str(feature).lower() for feature in device.get("features", [])} for device in devices]
    has_legacy = any(device.get("legacybranch") for device in devices)
    open_supported = not has_legacy and all("kernelopen" in device_features for device_features in features)
    proprietary_supported = all(
        bool(device.get("legacybranch"))
        or "kernelopen" not in device_features
        or "gsp_proprietary_supported" in device_features
        for device, device_features in zip(devices, features)
    )

    normalized = requested.lower()
    if normalized == "closed":
        normalized = "proprietary"
    if normalized not in ("auto", "open", "proprietary"):
        raise ValueError("nvidia_driver_module_flavor must be auto, open, or proprietary")
    if normalized == "auto":
        if has_legacy:
            if not proprietary_supported:
                raise ValueError("the detected GPUs do not share a compatible kernel module flavor")
            return "proprietary"
        if open_supported:
            return "open"
        if proprietary_supported:
            return "proprietary"
        raise ValueError("the detected GPUs do not share a compatible kernel module flavor")
    if normalized == "open" and not open_supported:
        raise ValueError("the open kernel module flavor is not compatible with every detected GPU")
    if normalized == "proprietary" and not proprietary_supported:
        raise ValueError("the proprietary kernel module flavor is not compatible with every detected GPU")
    return normalized


def _release_versions(releases: dict[str, Any], branch: str) -> list[str]:
    branch_data = releases.get(branch, {})
    versions = [
        release.get("release_version")
        for release in branch_data.get("driver_info", [])
        if release.get("release_version")
        and "x86_64" in release.get("architectures", [])
    ]
    return sorted(set(versions), key=version_key, reverse=True)


def _automatic_branch(
    releases: dict[str, Any],
    maximum_branch: int | None,
    branch_preference: str,
) -> str:
    def eligible(branch_type: str) -> list[str]:
        return sorted(
            [
                branch
                for branch, data in releases.items()
                if str(data.get("type", "")).lower() == branch_type
                and str(branch).isdigit()
                and (maximum_branch is None or int(branch) <= maximum_branch)
                and _release_versions(releases, str(branch))
            ],
            key=int,
            reverse=True,
        )

    if branch_preference not in ("lts", "production"):
        raise ValueError("branch_preference must be lts or production")
    if branch_preference == "lts":
        lts = eligible("lts branch")
        if lts:
            return lts[0]
    production = eligible("production branch")
    if production:
        return production[0]
    raise ValueError("no compatible LTS or production NVIDIA driver branch is available")


def resolve_driver(
    devices: list[dict[str, Any]],
    releases: dict[str, Any],
    supported_patch_versions: set[str],
    driver_version: str,
    driver_branch: str = "auto",
    module_flavor: str = "auto",
    branch_preference: str = "lts",
    patch_enabled: bool = True,
    minimum_legacy_branch: int = 580,
) -> dict[str, Any]:
    """Resolve the exact driver and policy for already-inventoried devices."""
    if not devices:
        raise ValueError("no NVIDIA display-class GPU was detected")
    unknown = [device for device in devices if device.get("unknown")]
    if unknown:
        ids = ", ".join("%s at %s" % (device["devid"], device["address"]) for device in unknown)
        raise ValueError("NVIDIA Driver Assistant does not recognize: %s" % ids)

    legacy_branches = []
    unsupported_legacy = []
    for device in devices:
        legacy = device.get("legacybranch")
        if not legacy:
            continue
        major = int(str(legacy).split(".", maxsplit=1)[0])
        legacy_branches.append(major)
        if major < minimum_legacy_branch:
            unsupported_legacy.append(device)
    if unsupported_legacy:
        details = ", ".join(
            "%s (%s, branch %s)" % (device["name"], device["devid"], device["legacybranch"])
            for device in unsupported_legacy
        )
        raise ValueError(
            "unsupported legacy NVIDIA hardware; Saltbox requires R%s or newer: %s"
            % (minimum_legacy_branch, details)
        )

    maximum_branch = min(legacy_branches) if legacy_branches else None
    geforce_present = any("geforce" in str(device.get("name", "")).lower() for device in devices)
    should_patch = bool(patch_enabled and geforce_present and driver_version.lower() != "ignore")
    selected_flavor = _module_flavor(devices, module_flavor)

    requested_version = driver_version.strip()
    requested_branch = driver_branch.strip().lower()
    if requested_version.lower() == "ignore":
        return {
            "devices": devices,
            "mode": "external",
            "resolved_version": "ignore",
            "resolved_branch": "external",
            "module_flavor": selected_flavor,
            "geforce_present": geforce_present,
            "patch_required": False,
            "driver_url": "",
            "checksum_url": "",
        }

    if requested_version.lower() == "latest":
        if requested_branch == "auto":
            selected_branch = _automatic_branch(releases, maximum_branch, branch_preference)
        else:
            if not requested_branch.isdigit():
                raise ValueError("nvidia_driver_branch must be auto or a quoted numeric branch")
            selected_branch = requested_branch
            if maximum_branch is not None and int(selected_branch) > maximum_branch:
                raise ValueError(
                    "driver branch %s exceeds the R%s hardware support ceiling"
                    % (selected_branch, maximum_branch)
                )
        candidates = _release_versions(releases, selected_branch)
        if should_patch:
            candidates = [version for version in candidates if version in supported_patch_versions]
        if not candidates:
            qualifier = " also supported by the pinned Keylase patch" if should_patch else ""
            raise ValueError(
                "no x86_64 driver release is available in branch %s%s"
                % (selected_branch, qualifier)
            )
        selected_version = candidates[0]
    else:
        version_key(requested_version)
        selected_version = requested_version
        selected_branch = requested_version.split(".", maxsplit=1)[0]
        if requested_branch != "auto" and requested_branch != selected_branch:
            raise ValueError(
                "exact driver %s conflicts with nvidia_driver_branch %s"
                % (selected_version, driver_branch)
            )
        if maximum_branch is not None and int(selected_branch) > maximum_branch:
            raise ValueError(
                "exact driver %s exceeds the R%s hardware support ceiling"
                % (selected_version, maximum_branch)
            )
        if should_patch and selected_version not in supported_patch_versions:
            raise ValueError(
                "driver %s is not supported by the pinned Keylase patch; choose a supported version "
                "or set nvidia_patch_enabled to false" % selected_version
            )

    filename = "NVIDIA-Linux-x86_64-%s.run" % selected_version
    base_url = "https://download.nvidia.com/XFree86/Linux-x86_64/%s" % selected_version
    return {
        "devices": devices,
        "mode": "managed",
        "resolved_version": selected_version,
        "resolved_branch": selected_branch,
        "module_flavor": selected_flavor,
        "geforce_present": geforce_present,
        "patch_required": should_patch,
        "driver_url": "%s/%s" % (base_url, filename),
        "checksum_url": "%s/%s.sha256sum" % (base_url, filename),
    }


def main() -> None:
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "catalog_path": {"type": "path", "required": True},
            "releases_path": {"type": "path", "required": True},
            "patch_path": {"type": "path", "required": False},
            "driver_version": {"type": "str", "required": True},
            "driver_branch": {"type": "str", "default": "auto"},
            "module_flavor": {"type": "str", "default": "auto"},
            "branch_preference": {
                "type": "str",
                "default": "lts",
                "choices": ["lts", "production"],
            },
            "patch_enabled": {"type": "bool", "default": True},
            "minimum_legacy_branch": {"type": "int", "default": 580},
            "sysfs_path": {"type": "path", "default": "/sys"},
            "supported_products_path": {"type": "path", "required": False},
        },
        supports_check_mode=True,
    )
    try:
        with open(module.params["catalog_path"], "r", encoding="utf-8") as stream:
            catalog = json.load(stream)
        with open(module.params["releases_path"], "r", encoding="utf-8") as stream:
            releases = json.load(stream)
        patch_content = _read_text(module.params["patch_path"]) if module.params["patch_path"] else ""
        devices = inventory_devices(module.params["sysfs_path"], catalog)
        result = resolve_driver(
            devices=devices,
            releases=releases,
            supported_patch_versions=patch_versions(patch_content),
            driver_version=module.params["driver_version"],
            driver_branch=module.params["driver_branch"],
            module_flavor=module.params["module_flavor"],
            branch_preference=module.params["branch_preference"],
            patch_enabled=module.params["patch_enabled"],
            minimum_legacy_branch=module.params["minimum_legacy_branch"],
        )
        if module.params["supported_products_path"] and result["mode"] == "managed":
            validate_supported_products(
                devices,
                _read_text(module.params["supported_products_path"]),
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        module.fail_json(msg=str(error))
    module.exit_json(changed=False, **result)


if __name__ == "__main__":
    main()
