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
      - Loading this plugin also installs Saltbox's ansible-core patches from
        C(saltbox_core_patches) in the root of the repository, once per process, whether or not
        the plugin is enabled. Each patch has its own switch, described in its module and in
        C(saltbox_core_patches/_gate.py).
    options:
      skip_bookkeeping_vars:
        description:
          - Switch of the C(skip_bookkeeping_vars) ansible-core patch, which builds no variables
            for the meta tasks ansible-core adds to every play by itself. Unset means on.
          - Read by the patch itself when it is installed, as an Ansible boolean (yes/no,
            true/false, on/off, y/n, t/f, 1/0, any case). Any other value keeps the patch off and
            prints a warning. The environment variable takes precedence over C(ansible.cfg), and
            an empty one counts as unset; runs started through C(sb) only see C(ansible.cfg), as
            sb drops the environment. Saltbox's own C(ansible.cfg) is tracked by git, so an edit
            there lasts until the next update.
        type: str
        ini:
          - section: saltbox_core_patches
            key: skip_bookkeeping_vars
        env:
          - name: SALTBOX_SKIP_BOOKKEEPING_VARS
      tag_vars_dict_only:
        description:
          - Switch of the C(tag_vars_dict_only) ansible-core patch, which tags only the
            deprecated C(vars) dictionary instead of a copy of every value in it. Unset means on.
          - Read by the patch itself when it is installed, as an Ansible boolean (yes/no,
            true/false, on/off, y/n, t/f, 1/0, any case). Any other value keeps the patch off and
            prints a warning. The environment variable takes precedence over C(ansible.cfg), and
            an empty one counts as unset; runs started through C(sb) only see C(ansible.cfg), as
            sb drops the environment. Saltbox's own C(ansible.cfg) is tracked by git, so an edit
            there lasts until the next update.
        type: str
        ini:
          - section: saltbox_core_patches
            key: tag_vars_dict_only
        env:
          - name: SALTBOX_TAG_VARS_DICT_ONLY
      share_lazy_var_wrappers:
        description:
          - Switch of the C(share_lazy_var_wrappers) ansible-core patch, which reuses the lazy
            wrappers of a large, unchanged variable layer across the templates of a task instead
            of wrapping every variable again for each template. Unset means on.
          - Read by the patch itself when it is installed, as an Ansible boolean (yes/no,
            true/false, on/off, y/n, t/f, 1/0, any case). Any other value keeps the patch off and
            prints a warning. The environment variable takes precedence over C(ansible.cfg), and
            an empty one counts as unset; runs started through C(sb) only see C(ansible.cfg), as
            sb drops the environment. Saltbox's own C(ansible.cfg) is tracked by git, so an edit
            there lasts until the next update.
        type: str
        ini:
          - section: saltbox_core_patches
            key: share_lazy_var_wrappers
        env:
          - name: SALTBOX_SHARE_LAZY_VAR_WRAPPERS
    author: salty
    requirements:
      - Enabled in configuration
"""

import importlib.util
import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

# ansible-core ships no py.typed marker, so its imports are untyped.
from ansible.errors import AnsibleParserError  # type: ignore[import-untyped]
from ansible.inventory.group import InventoryObjectType  # type: ignore[import-untyped]
from ansible.plugins.vars import BaseVarsPlugin  # type: ignore[import-untyped]
from ansible.utils.display import Display  # type: ignore[import-untyped]
from ansible.utils.vars import combine_vars  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from ansible.inventory.group import Group  # type: ignore[import-untyped]
    from ansible.inventory.host import Host  # type: ignore[import-untyped]
    from ansible.parsing.dataloader import DataLoader  # type: ignore[import-untyped]

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
CORE_PATCHES = "saltbox_core_patches"


def install_core_patches() -> None:
    """Import the saltbox_core_patches package of this repository once per process."""
    if CORE_PATCHES in sys.modules:
        return
    package_dir = os.path.join(SALTBOX_ROOT, CORE_PATCHES)
    try:
        spec = importlib.util.spec_from_file_location(
            CORE_PATCHES,
            os.path.join(package_dir, "__init__.py"),
            submodule_search_locations=[package_dir],
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {package_dir}")
        package = importlib.util.module_from_spec(spec)
        sys.modules[CORE_PATCHES] = package
        spec.loader.exec_module(package)
        package.install_all()
    except Exception as ex:  # noqa: BLE001 - any failure must leave ansible-core's own code running
        Display().warning(f"[{CORE_PATCHES}] not installed: {ex}")


install_core_patches()


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
