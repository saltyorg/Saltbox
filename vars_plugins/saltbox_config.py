from __future__ import annotations

DOCUMENTATION = """
    name: saltbox_config
    short_description: Loads the Saltbox config files as host variables
    description:
      - Loads the Saltbox config files C(accounts.yml), C(settings.yml), C(adv_settings.yml),
        C(backup_config.yml), C(providers.yml) and C(hetzner_vlan.yml) from the root of the
        Saltbox repository that contains this plugin, and adds them to the variables of each host
        in that repository's C(inventories) directory.
      - A config file that does not exist is replaced by its shipped default,
        C(defaults/<name>.yml.default). A file is skipped when neither exists, and an empty file
        adds no variables.
      - Each file must contain a dictionary of variables or a list of dictionaries, as with play
        C(vars_files).
      - The files are merged in the order listed above, so a later file's top-level key replaces
        the same key from an earlier file.
      - Each file is parsed once per run. Changes made to a config file during a run are not seen
        until the next run.
      - Enable it after C(host_group_vars) in C(vars_plugins_enabled), so the config files take
        precedence over C(inventories/host_vars/localhost.yml). Facts, play variables, role
        variables, C(include_vars), C(set_fact) and extra variables take precedence over them.
    notes:
      - The config values are host variables, so they also appear in C(hostvars) and in
        C(ansible-inventory --host) output, secrets included.
      - Inventories outside the Saltbox repository's C(inventories) directory receive no config
        variables.
    author: salty
    requirements:
      - Enabled in configuration
"""

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ansible.errors import AnsibleParserError
from ansible.inventory.group import InventoryObjectType
from ansible.plugins.vars import BaseVarsPlugin
from ansible.utils.vars import combine_vars

if TYPE_CHECKING:
    from ansible.inventory.group import Group
    from ansible.inventory.host import Host
    from ansible.parsing.dataloader import DataLoader

SALTBOX_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
INVENTORY_DIR = os.path.join(SALTBOX_ROOT, "inventories")
CONFIG_FILES = (
    "accounts",
    "settings",
    "adv_settings",
    "backup_config",
    "providers",
    "hetzner_vlan",
)

_IS_INVENTORY_DIR: dict[str, bool] = {}


def _is_inventory_dir(path: str) -> bool:
    try:
        return _IS_INVENTORY_DIR[path]
    except KeyError:
        result = _IS_INVENTORY_DIR[path] = os.path.realpath(path) == INVENTORY_DIR
        return result


def _config_file(name: str) -> str | None:
    for candidate in (
        os.path.join(SALTBOX_ROOT, f"{name}.yml"),
        os.path.join(SALTBOX_ROOT, "defaults", f"{name}.yml.default"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


class VarsModule(BaseVarsPlugin):
    """Return the merged Saltbox config files as host variables."""

    REQUIRES_ENABLED = True
    is_stateless = True

    def get_vars(
        self,
        loader: DataLoader,
        path: str,
        entities: list[Host | Group] | Host | Group,
        cache: bool = True,
    ) -> dict[str, Any]:
        """Merge the config files for hosts in the Saltbox inventory directory."""
        if not isinstance(entities, list):
            entities = [entities]
        if not any(
            getattr(entity, "base_type", None) is InventoryObjectType.HOST
            for entity in entities
        ):
            return {}
        if not _is_inventory_dir(path):
            return {}

        data: dict[str, Any] = {}
        for name in CONFIG_FILES:
            found = _config_file(name)
            if found is None:
                continue
            new_data = loader.load_from_file(
                found, cache="all", unsafe=True, trusted_as_template=True
            )
            if new_data is None:
                continue
            items = new_data if isinstance(new_data, list) else [new_data]
            for item in items:
                if not isinstance(item, Mapping):
                    raise AnsibleParserError(
                        message="Invalid variable file contents.",
                        obj=item,
                        help_text=(
                            "Variable files must contain either a dictionary of"
                            " variables, or a list of dictionaries."
                        ),
                    )
                data = combine_vars(data, item)
        return data
