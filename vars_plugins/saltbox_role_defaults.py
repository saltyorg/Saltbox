from __future__ import annotations

DOCUMENTATION = """
    name: saltbox_role_defaults
    short_description: Loads role defaults as variables of the all group
    description:
      - Loads C(defaults/main.yml) of every role in O(roles_paths) and adds them to the variables
        of the C(all) group in the C(inventories) directory of the Saltbox repository that
        contains this plugin.
      - Lets playbooks outside the Saltbox repository, such as Sandbox and saltbox_mod, use the
        role defaults of roles that are not part of their play.
      - Directories are read in the listed order and roles alphabetically within each, so a later
        file's top-level key replaces the same key from an earlier file.
      - Each file is parsed once per run. Changes made to a defaults file during a run are not
        seen until the next run.
      - Enable it before C(host_group_vars) in C(vars_plugins_enabled), so
        C(inventories/group_vars/all.yml) takes precedence over the role defaults. Host
        variables such as C(inventories/host_vars/localhost.yml), play variables, role
        variables, block and task variables, C(include_vars), C(set_fact) and extra variables
        also take precedence over them.
    options:
      roles_paths:
        description:
          - Role directories whose role defaults are loaded.
          - Defaults to the C(roles) directory of the Saltbox repository that contains this
            plugin.
        type: list
        elements: path
        ini:
          - section: saltbox_role_defaults
            key: roles_paths
        env:
          - name: SALTBOX_ROLE_DEFAULTS_ROLES_PATHS
    notes:
      - The defaults are group variables, so they also appear in C(hostvars) and in
        C(ansible-inventory) output.
      - Inventories outside the Saltbox repository's C(inventories) directory receive no
        role defaults.
    author: salty
    requirements:
      - Enabled in configuration
"""

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

# ansible-core ships no py.typed marker, so these imports are untyped.
from ansible.errors import AnsibleParserError  # type: ignore[import-untyped]
from ansible.inventory.group import InventoryObjectType  # type: ignore[import-untyped]
from ansible.plugins.vars import BaseVarsPlugin  # type: ignore[import-untyped]
from ansible.utils.vars import combine_vars  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from ansible.inventory.group import Group  # type: ignore[import-untyped]
    from ansible.inventory.host import Host  # type: ignore[import-untyped]
    from ansible.parsing.dataloader import DataLoader  # type: ignore[import-untyped]

SALTBOX_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
INVENTORY_DIR = os.path.join(SALTBOX_ROOT, "inventories")
DEFAULT_ROLES_PATHS = (os.path.join(SALTBOX_ROOT, "roles"),)

_IS_INVENTORY_DIR: dict[str, bool] = {}
_LOADED: dict[tuple[str, ...], dict[str, Any]] = {}


def _is_inventory_dir(path: str) -> bool:
    try:
        return _IS_INVENTORY_DIR[path]
    except KeyError:
        result = _IS_INVENTORY_DIR[path] = os.path.realpath(path) == INVENTORY_DIR
        return result


def _defaults_files(roles_path: str) -> list[str]:
    try:
        names = sorted(os.listdir(roles_path))
    except FileNotFoundError:
        return []
    files = []
    for name in names:
        candidate = os.path.join(roles_path, name, "defaults", "main.yml")
        if os.path.isfile(candidate):
            files.append(candidate)
    return files


def _load(loader: DataLoader, roles_paths: tuple[str, ...]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for roles_path in roles_paths:
        for found in _defaults_files(roles_path):
            new_data = loader.load_from_file(
                found, cache="all", unsafe=True, trusted_as_template=True
            )
            if new_data is None:
                continue
            if not isinstance(new_data, Mapping):
                raise AnsibleParserError(
                    message=f"Invalid role defaults file {found!r}.",
                    obj=new_data,
                    help_text="Role defaults files must contain a dictionary of variables.",
                )
            data = combine_vars(data, new_data)
    return data


class VarsModule(BaseVarsPlugin):
    """Return the merged role defaults as variables of the all group."""

    REQUIRES_ENABLED = True
    is_stateless = True

    def get_vars(
        self,
        loader: DataLoader,
        path: str,
        entities: list[Host | Group] | Host | Group,
        cache: bool = True,
    ) -> dict[str, Any]:
        """Return the role defaults for the all group of the Saltbox inventory."""
        if not isinstance(entities, list):
            entities = [entities]
        if not any(
            getattr(entity, "base_type", None) is InventoryObjectType.GROUP
            and entity.name == "all"
            for entity in entities
        ):
            return {}
        if not _is_inventory_dir(path):
            return {}

        configured = self.get_option("roles_paths") or DEFAULT_ROLES_PATHS
        roles_paths = tuple(os.path.realpath(roles_path) for roles_path in configured)
        try:
            return _LOADED[roles_paths]
        except KeyError:
            result = _LOADED[roles_paths] = _load(loader, roles_paths)
            return result
