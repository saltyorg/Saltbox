#!/usr/bin/env python3

"""Build the role matrix used by Saltbox CI workflows."""

import argparse
import json
import re
from pathlib import Path

from ruamel.yaml import YAML


def parse_args():
    parser = argparse.ArgumentParser(description="Build the Saltbox CI role matrix.")
    parser.add_argument("--os", nargs="*", dest="operating_systems")
    parser.add_argument("--playbook", type=Path, default=Path("saltbox.yml"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(".github/ci-role-matrix.yml"),
    )
    return parser.parse_args()


def load_ignored_tags(config_path):
    config = YAML(typ="safe").load(config_path)
    ignored_tags = config["ignored_tags"]

    if not all(isinstance(tag, str) for tag in ignored_tags):
        raise ValueError("All ignored tags must be strings.")

    return set(ignored_tags)


def load_role_tags(playbook_path):
    yaml = YAML(typ="safe")
    role_tags = set()
    in_role_matrix = False

    for line in playbook_path.read_text().splitlines():
        if "# Core" in line:
            in_role_matrix = True
            continue
        if "# Apps End" in line:
            break
        if not in_role_matrix or line.lstrip().startswith("#"):
            continue

        match = re.search(r"\btags:\s*(\[[^\]]*])", line)
        if match:
            role_tags.update(yaml.load(match.group(1)))

    return role_tags


def main():
    args = parse_args()
    role_tags = load_role_tags(args.playbook)
    ignored_tags = load_ignored_tags(args.config)
    matrix = {"roles": sorted(role_tags - ignored_tags)}

    if args.operating_systems:
        matrix["os"] = args.operating_systems

    print(json.dumps(matrix, separators=(",", ":")))


if __name__ == "__main__":
    main()
