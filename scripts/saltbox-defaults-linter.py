#!/usr/bin/env python3
"""
Saltbox Role Defaults Linter
Enforces Saltbox formatting rules for role defaults/main.yml files

Rules:
1. Operator Alignment: | and + operators must align with first character after "{{
   - Standard: All operators align with base position (first char after "{{ )
   - Exception: When 'else' is followed by content on a new line that continues,
     subsequent operators within that else branch align with content after 'else '
   - Context resets when if/else blocks close (marked by )) or )))

2. If/Else Alignment: if and else keywords must align with the opening {{ brackets,
   the innermost unmatched grouping (, or the content inside a function call

3. Variable Prefix: top-level *_role_* defaults must start with the role
   directory name

4. Docker Layer Composition: aggregates with matching _default and _custom
   companions must use explicit role_var lookups in default-before-custom order

5. Web URL Composition: role web URLs with matching subdomain and domain
   defaults must access both through explicit role_var lookups

6. Docker Image Composition: role images must define repository and tag
   defaults and access both through explicit role_var lookups

7. Explicit Role Target: every role_var lookup in defaults must specify role=

8. Docker Network Formula: network aggregates must use either the standard
   common/default/custom formula or the supported interface-pinned variant

9. Section Structure: canonical major sections must not repeat and must appear
   in their established relative order

10. Web URL Literal Prefix: standard web URLs must keep literal https://
    outside their Jinja expression

11. Docker Envs Custom Usage: _docker_envs_custom may only be the final
    combine layer of its corresponding Docker environment aggregate

12. Lookup Documentation: role-local computed defaults ending in _lookup must
    have a supported documentation-exclusion directive immediately above them

13. Redundant Docker Layers: non-network default/custom pairs may not both be
    empty when their aggregate only combines those two layers

14. Docker Hosts Formula: host mappings must compose only their role-local
    default and custom layers

15. Traefik API Router Contract: every role with Traefik enabled must define
    the complete API router toggle, endpoint, and ordered middleware layers and
    must not use legacy secure API middleware variable names

16. Nested Traefik Adapter Contract: roles that expose a namespaced web adapter
    through an included role must declare and forward the complete namespaced
    regular and API router contract

17. Docker Helper Prefix Argument: shared Docker lifecycle helpers accept
    var_prefix as their public argument

18. Non-Docker Traefik Renderer Contract: roles that declare Traefik without
    creating a shared-label Docker container must render their API contract
"""

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import override

ParenthesisContext = tuple[int, str]
JinjaBlock = tuple[int, int, list[str]]
TopLevelVariable = tuple[str, int, list[str]]
DEFAULTS_SECTION_ORDER = (
    "Basics",
    "Settings",
    "Postgres",
    "Paths",
    "Web",
    "DNS",
    "Traefik",
    "Docker",
    "Dependencies",
)


class LintError:
    """Represents a single linting error"""

    def __init__(
        self,
        file: str,
        line: int,
        message: str,
        repo_url: str | None = None,
        commit_sha: str | None = None,
    ) -> None:
        self.file: str = file
        self.line: int = line
        self.message: str = message
        self.repo_url: str | None = repo_url
        self.commit_sha: str | None = commit_sha

    def to_github_annotation(self) -> str:
        """
        Format error as GitHub Actions annotation

        Includes a clickable link to the file at the specific commit in the message,
        since GitHub's default annotation links only point to the commit page
        (which doesn't show the file if it wasn't modified in that commit).
        """
        # Build GitHub blob URL if we have repo and commit info
        if self.repo_url and self.commit_sha:
            # Format: https://github.com/owner/repo/blob/commit_sha/path/to/file.yml#L123
            github_link = (
                f"{self.repo_url}/blob/{self.commit_sha}/{self.file}#L{self.line}"
            )
            message_with_link = f"{self.message} - {github_link}"
        else:
            message_with_link = self.message

        return f"::error file={self.file},line={self.line}::{message_with_link}"

    def github_link(self) -> str:
        """Return a direct link to this finding when running in GitHub Actions."""
        if not self.repo_url or not self.commit_sha:
            return ""
        return f"{self.repo_url}/blob/{self.commit_sha}/{self.file}#L{self.line}"

    @override
    def __str__(self) -> str:
        return f"{self.file}:{self.line} - {self.message}"


class DefaultsLinter:
    """Lints a single defaults/main.yml file"""

    def __init__(
        self,
        file_path: Path,
        repo_url: str | None = None,
        commit_sha: str | None = None,
    ) -> None:
        self.file_path: Path = file_path
        self.repo_url: str | None = repo_url
        self.commit_sha: str | None = commit_sha
        self.lines: list[str] = file_path.read_text(encoding="utf-8").splitlines()
        self.errors: list[LintError] = []

    def check_operator_alignment(self) -> None:
        """
        Rule 1: Check | and + operators align with first character after '{{ '

        Standard alignment - all operators at base position:
            sonarr_role_docker_envs: "{{ lookup('role_var', '_docker_envs_default', role='sonarr')
                                         | combine(lookup('role_var', '_docker_envs_custom', role='sonarr')) }}"
                                         ^ Aligns with 'l' in lookup (position after "{{ )

        Multiple operators - all at same base position:
            wikijs_role_docker_networks: "{{ docker_networks_common
                                             + lookup('role_var', '_docker_networks_default', role='wikijs')
                                             + lookup('role_var', '_docker_networks_custom', role='wikijs') }}"
                                             ^ All + align with 'd' in docker_networks_common

        Exception - else with continuing content creates new context:
            dozzle_role_docker_commands: "{{ lookup('role_var', '_docker_commands_agent', role='dozzle')
                                             + lookup('role_var', '_docker_commands_default', role='dozzle')
                                          if lookup('role_var', '_agent_mode', role='dozzle')
                                          else lookup('role_var', '_docker_commands_default', role='dozzle')
                                               + lookup('role_var', '_docker_commands_custom', role='dozzle') }}"
                                               ^ This + aligns with 'l' in lookup after 'else ', not with base

        Inline if/else (no context change):
            traefik_role_docker_labels: "{{ docker_labels_saltbox
                                            | combine((lookup('role_var', '_docker_labels_http', role='traefik')
                                                      if traefik_http
                                                      else lookup('role_var', '_docker_labels_dns', role='traefik')))
                                            | combine(lookup('role_var', '_docker_labels_custom', role='traefik')) }}"
                                            ^ All | remain at base position - inline if/else doesn't change context
        """
        for (
            jinja_start_line,
            jinja_bracket_indent,
            jinja_lines,
        ) in self.iter_multiline_jinja_blocks():
            first_line = jinja_lines[0]
            variable_match = re.match(r"^([a-z][a-z0-9_]*):(?:\s|$)", first_line)
            if not variable_match:
                continue

            # Operator alignment applies to top-level defaults expressions that
            # begin on the variable's definition line. Multiline Jinja embedded
            # in a block scalar retains its existing formatting semantics.
            if jinja_bracket_indent < variable_match.end():
                continue

            expression_start = jinja_bracket_indent + len("{{")
            if not first_line.startswith(" ", expression_start):
                continue

            var_name = variable_match.group(1)
            expected_alignment = expression_start + 1
            current_alignment = expected_alignment
            in_else_context = False

            try:
                relative_path = str(self.file_path.relative_to(Path.cwd()))
            except ValueError:
                relative_path = str(self.file_path)

            for line_offset, continuation_line in enumerate(jinja_lines[1:], 1):
                # Closing an if/else block resets alignment context to the base.
                if in_else_context and re.search(
                    r"\)\)(?:\)|$)", continuation_line.rstrip()
                ):
                    current_alignment = expected_alignment
                    in_else_context = False

                # An else branch whose content continues on following lines creates
                # a temporary alignment context for those continuation operators.
                else_match = re.match(
                    r"^(\s+)else (lookup|[a-z_]+)", continuation_line
                )
                if else_match:
                    closes_immediately = re.search(r"\)\)\)", continuation_line)
                    if not closes_immediately:
                        current_alignment = len(else_match.group(1)) + len("else ")
                        in_else_context = True

                op_match = re.match(r"^(\s+)([|+]) ", continuation_line)
                if not op_match:
                    continue

                actual_spaces = len(op_match.group(1))
                operator = op_match.group(2)
                if actual_spaces == current_alignment:
                    continue

                diff = actual_spaces - current_alignment
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=jinja_start_line + line_offset,
                        message=f"[operator-alignment] Variable '{var_name}': Operator '{operator}' at column {actual_spaces}, expected {current_alignment} (off by {diff:+d})",
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )

    def check_variable_prefix(self) -> None:
        """Rule 3: Check top-level role defaults use the role's prefix."""
        role_name = self.file_path.parent.parent.name
        expected_prefix = f"{role_name}_"

        for line_number, line in enumerate(self.lines, 1):
            match = re.match(r"^([a-z][a-z0-9_]*):(?:\s|$)", line)
            if not match:
                continue

            variable_name = match.group(1)
            if "_role_" not in variable_name:
                continue

            if variable_name.startswith(expected_prefix):
                continue

            try:
                relative_path = str(self.file_path.relative_to(Path.cwd()))
            except ValueError:
                relative_path = str(self.file_path)

            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=f"[variable-prefix] Variable '{variable_name}' must start with '{expected_prefix}'",
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )

    def check_docker_layer_composition(self) -> None:
        """Rule 4: Check explicit, ordered access to Docker aggregate layers."""
        role_name = self.file_path.parent.parent.name
        docker_prefix = f"{role_name}_role_docker_"
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        for variable_name, (line_number, variable_lines) in variables.items():
            if not variable_name.startswith(docker_prefix) or variable_name.endswith(
                ("_default", "_custom")
            ):
                continue

            default_name = f"{variable_name}_default"
            custom_name = f"{variable_name}_custom"
            if default_name not in variables or custom_name not in variables:
                continue

            lookup_base = variable_name.removeprefix(f"{role_name}_role")
            default_lookup = self.explicit_role_var_lookup_pattern(
                f"{lookup_base}_default", role_name
            )
            custom_lookup = self.explicit_role_var_lookup_pattern(
                f"{lookup_base}_custom", role_name
            )
            expression = "\n".join(variable_lines)
            default_match = default_lookup.search(expression)
            custom_match = custom_lookup.search(expression)

            missing_layers: list[str] = []
            if default_match is None:
                missing_layers.append("_default")
            if custom_match is None:
                missing_layers.append("_custom")

            if missing_layers:
                message = (
                    f"[docker-layer-composition] Variable '{variable_name}' must "
                    "access its "
                    f"{' and '.join(missing_layers)} layer(s) with explicit "
                    f"role_var lookups using role='{role_name}'"
                )
            elif default_match.start() > custom_match.start():
                message = (
                    f"[docker-layer-composition] Variable '{variable_name}' must "
                    "access its _default layer before its _custom layer"
                )
            else:
                continue

            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=message,
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )

    def check_web_url_composition(self) -> None:
        """Rule 5: Check explicit access to web URL subdomain and domain defaults."""
        role_name = self.file_path.parent.parent.name
        web_url_name = f"{role_name}_role_web_url"
        web_subdomain_name = f"{role_name}_role_web_subdomain"
        web_domain_name = f"{role_name}_role_web_domain"
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        if not all(
            variable_name in variables
            for variable_name in (web_url_name, web_subdomain_name, web_domain_name)
        ):
            return

        line_number, variable_lines = variables[web_url_name]
        expression = "\n".join(variable_lines)
        missing_components = [
            suffix
            for suffix in ("_web_subdomain", "_web_domain")
            if self.explicit_role_var_lookup_pattern(
                suffix, role_name
            ).search(expression)
            is None
        ]
        if not missing_components:
            return

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        self.errors.append(
            LintError(
                file=relative_path,
                line=line_number,
                message=(
                    f"[web-url-composition] Variable '{web_url_name}' must access "
                    f"its {' and '.join(missing_components)} default(s) with "
                    f"explicit role_var lookups using role='{role_name}'"
                ),
                repo_url=self.repo_url,
                commit_sha=self.commit_sha,
            )
        )

    def check_web_url_literal_prefix(self) -> None:
        """Rule 10: Check standard web URLs start with literal https://."""
        role_name = self.file_path.parent.parent.name
        web_url_name = f"{role_name}_role_web_url"
        required_variables = (
            web_url_name,
            f"{role_name}_role_web_subdomain",
            f"{role_name}_role_web_domain",
        )
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        if not all(variable_name in variables for variable_name in required_variables):
            return

        line_number, variable_lines = variables[web_url_name]
        if re.match(
            "^" + re.escape(web_url_name) + r":\s*['\"]https://\{\{",
            variable_lines[0],
        ):
            return

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        self.errors.append(
            LintError(
                file=relative_path,
                line=line_number,
                message=(
                    f"[web-url-literal-prefix] Variable '{web_url_name}' must "
                    "keep literal 'https://' outside the Jinja expression"
                ),
                repo_url=self.repo_url,
                commit_sha=self.commit_sha,
            )
        )

    def check_docker_image_composition(self) -> None:
        """Rule 6: Check explicit access to Docker image repository and tag."""
        role_name = self.file_path.parent.parent.name
        image_name = f"{role_name}_role_docker_image"
        image_repo_name = f"{role_name}_role_docker_image_repo"
        image_tag_name = f"{role_name}_role_docker_image_tag"
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        if image_name not in variables:
            return

        line_number, variable_lines = variables[image_name]
        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        missing_defaults = [
            variable_name
            for variable_name in (image_repo_name, image_tag_name)
            if variable_name not in variables
        ]
        if missing_defaults:
            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=(
                        f"[docker-image-composition] Variable '{image_name}' must "
                        f"define companion default(s): {', '.join(missing_defaults)}"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )
            return

        expression = "\n".join(variable_lines)
        missing_components = [
            suffix
            for suffix in ("_docker_image_repo", "_docker_image_tag")
            if self.explicit_role_var_lookup_pattern(
                suffix, role_name
            ).search(expression)
            is None
        ]
        if not missing_components:
            return

        self.errors.append(
            LintError(
                file=relative_path,
                line=line_number,
                message=(
                    f"[docker-image-composition] Variable '{image_name}' must "
                    f"access its {' and '.join(missing_components)} default(s) "
                    f"with explicit role_var lookups using role='{role_name}'"
                ),
                repo_url=self.repo_url,
                commit_sha=self.commit_sha,
            )
        )

    def check_explicit_role_var_target(self) -> None:
        """Rule 7: Check every role_var lookup specifies its target role."""
        source = "\n".join(self.lines)
        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        for call_start, call in self.iter_lookup_calls(source):
            if re.match(r"lookup\s*\(\s*(['\"])role_var\1\s*,", call) is None:
                continue
            if self.call_has_keyword_argument(call, "role"):
                continue

            line_start = source.rfind("\n", 0, call_start) + 1
            if source[line_start:call_start].lstrip().startswith("#"):
                continue

            self.errors.append(
                LintError(
                    file=relative_path,
                    line=source.count("\n", 0, call_start) + 1,
                    message=(
                        "[role-var-target] role_var lookup must specify an "
                        "explicit role= target"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )

    def check_docker_network_formula(self) -> None:
        """Rule 8: Check Docker networks use a supported composition variant."""
        role_name = self.file_path.parent.parent.name
        networks_name = f"{role_name}_role_docker_networks"
        networks_default_name = f"{networks_name}_default"
        networks_custom_name = f"{networks_name}_custom"
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        if networks_name not in variables:
            return

        line_number, variable_lines = variables[networks_name]
        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        missing_defaults = [
            variable_name
            for variable_name in (networks_default_name, networks_custom_name)
            if variable_name not in variables
        ]
        if missing_defaults:
            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=(
                        f"[docker-network-formula] Variable '{networks_name}' must "
                        f"define companion default(s): {', '.join(missing_defaults)}"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )
            return

        expression = self.variable_jinja_expression(line_number, variable_lines)
        default_lookup = self.explicit_role_var_lookup_pattern(
            "_docker_networks_default", role_name
        ).search(expression)
        custom_lookup = self.explicit_role_var_lookup_pattern(
            "_docker_networks_custom", role_name
        ).search(expression)
        if default_lookup is None or custom_lookup is None:
            return

        prefix = expression[: default_lookup.start()]
        between_lookups = expression[default_lookup.end() : custom_lookup.start()]
        suffix = expression[custom_lookup.end() :]
        standard_prefix = re.compile(
            "^"
            + re.escape(networks_name)
            + r":\s*['\"]\{\{\s*docker_networks_common\s*\+\s*"
        )
        pinned_prefix = re.compile(
            "^"
            + re.escape(networks_name)
            + r":\s*['\"]\{\{\s*\(\s*docker_networks_common\s*"
            r"\|\s*map\(\s*['\"]combine['\"]\s*,\s*"
            r"\{\s*['\"]driver_opts['\"]\s*:\s*"
            r"\{\s*['\"]com\.docker\.network\.endpoint\.ifname['\"]\s*:\s*"
            r"['\"](?P<common_interface>[^'\"]+)['\"]\s*\}\s*\}\s*\)\s*"
            r"\|\s*list\s*\)\s*\+\s*"
        )
        standard_match = standard_prefix.fullmatch(prefix)
        pinned_match = pinned_prefix.fullmatch(prefix)
        has_standard_tail = (
            re.fullmatch(r"\s*\+\s*", between_lookups) is not None
            and re.fullmatch(r"\s*\}\}['\"]\s*", suffix) is not None
        )

        message: str | None = None
        if not has_standard_tail or (standard_match is None and pinned_match is None):
            message = (
                f"[docker-network-formula] Variable '{networks_name}' must use "
                "the standard or interface-pinned network composition variant"
            )
        elif pinned_match is not None:
            _, default_lines = variables[networks_default_name]
            default_interfaces = re.findall(
                r"^\s+com\.docker\.network\.endpoint\.ifname:\s*"
                r"['\"]?([^'\"\s#]+)",
                "\n".join(default_lines),
                flags=re.MULTILINE,
            )
            common_interface = pinned_match.group("common_interface")
            if not default_interfaces:
                message = (
                    f"[docker-network-formula] Variable '{networks_default_name}' "
                    "must pin its application network interface when the common "
                    "networks are interface-pinned"
                )
            elif common_interface in default_interfaces:
                message = (
                    f"[docker-network-formula] Variable '{networks_name}' must use "
                    "distinct interface names for common and application networks"
                )

        if message is None:
            return

        self.errors.append(
            LintError(
                file=relative_path,
                line=line_number,
                message=message,
                repo_url=self.repo_url,
                commit_sha=self.commit_sha,
            )
        )

    def check_section_structure(self) -> None:
        """Rule 9: Check canonical sections are unique and relatively ordered."""
        section_positions = {
            section_name: position
            for position, section_name in enumerate(DEFAULTS_SECTION_ORDER)
        }
        seen_sections: dict[str, int] = {}
        previous_section: str | None = None
        previous_position = -1

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        for line_index, line in enumerate(self.lines):
            match = re.fullmatch(r"# ([A-Za-z][A-Za-z0-9 ]*)", line)
            if match is None:
                continue

            if (
                line_index == 0
                or line_index + 1 >= len(self.lines)
                or self.lines[line_index - 1] != "################################"
                or self.lines[line_index + 1] != "################################"
            ):
                continue

            section_name = match.group(1)
            if section_name not in section_positions:
                continue

            line_number = line_index + 1

            if section_name in seen_sections:
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=line_number,
                        message=(
                            f"[section-structure] Duplicate '{section_name}' "
                            f"section; first declared at line "
                            f"{seen_sections[section_name]}"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )
                continue

            seen_sections[section_name] = line_number
            current_position = section_positions[section_name]
            if current_position < previous_position:
                assert previous_section is not None
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=line_number,
                        message=(
                            f"[section-structure] Section '{section_name}' must "
                            f"appear before '{previous_section}'"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )
                continue

            previous_section = section_name
            previous_position = current_position

    def check_docker_envs_custom_usage(self) -> None:
        """Rule 11: Check Docker custom envs remain the final override layer."""
        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        role_var_custom_pattern = re.compile(
            r"lookup\s*\(\s*(['\"])role_var\1\s*,\s*"
            r"(['\"])_docker_envs_custom\2\s*,"
        )
        direct_custom_pattern = re.compile(
            r"\b([a-z][a-z0-9_]*)_role_docker_envs_custom\b"
        )

        for variable_name, line_number, variable_lines in self.iter_top_level_variables():
            expression = "\n".join(variable_lines)

            for line_offset, line in enumerate(variable_lines):
                if line.lstrip().startswith("#"):
                    continue
                for direct_match in direct_custom_pattern.finditer(line):
                    direct_name = direct_match.group(0)
                    if line_offset == 0 and variable_name == direct_name:
                        continue
                    self.errors.append(
                        LintError(
                            file=relative_path,
                            line=line_number + line_offset,
                            message=(
                                f"[docker-envs-custom-usage] Variable '{direct_name}' "
                                "must only be accessed through an explicit role_var "
                                "lookup in its final Docker environment aggregate"
                            ),
                            repo_url=self.repo_url,
                            commit_sha=self.commit_sha,
                        )
                    )

            for call_start, call in self.iter_lookup_calls(expression):
                if role_var_custom_pattern.match(call) is None:
                    continue

                call_line_start = expression.rfind("\n", 0, call_start) + 1
                if expression[call_line_start:call_start].lstrip().startswith("#"):
                    continue

                call_line = line_number + expression.count("\n", 0, call_start)
                role_match = re.search(
                    r",\s*role\s*=\s*(['\"])([a-z][a-z0-9_]*)\1", call
                )
                if role_match is None:
                    self.errors.append(
                        LintError(
                            file=relative_path,
                            line=call_line,
                            message=(
                                "[docker-envs-custom-usage] _docker_envs_custom "
                                "lookup must use a literal role target"
                            ),
                            repo_url=self.repo_url,
                            commit_sha=self.commit_sha,
                        )
                    )
                    continue

                target_role = role_match.group(2)
                expected_aggregate = f"{target_role}_role_docker_envs"
                if variable_name != expected_aggregate:
                    self.errors.append(
                        LintError(
                            file=relative_path,
                            line=call_line,
                            message=(
                                "[docker-envs-custom-usage] _docker_envs_custom for "
                                f"role '{target_role}' may only be accessed in "
                                f"'{expected_aggregate}'"
                            ),
                            repo_url=self.repo_url,
                            commit_sha=self.commit_sha,
                        )
                    )
                    continue

                before_lookup = expression[:call_start]
                after_lookup = expression[call_start + len(call) :]
                if re.search(r"\|\s*combine\(\s*$", before_lookup) and re.match(
                    r"\s*\)\s*\}\}['\"]", after_lookup
                ):
                    continue

                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=call_line,
                        message=(
                            "[docker-envs-custom-usage] _docker_envs_custom must be "
                            "the final combine layer of its Docker environment "
                            "aggregate"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )

    def check_lookup_documentation(self) -> None:
        """Rule 12: Check role-local lookup defaults are excluded from docs."""
        role_name = self.file_path.parent.parent.name
        exclusion_directives = {
            "# Skip docs",
            "# Do not edit or override using the inventory",
        }
        variable_pattern = re.compile(
            "^" + re.escape(role_name) + r"_role_[a-z0-9_]*_lookup$"
        )

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        for variable_name, line_number, _ in self.iter_top_level_variables():
            if variable_pattern.fullmatch(variable_name) is None:
                continue
            if (
                line_number > 1
                and self.lines[line_number - 2] in exclusion_directives
            ):
                continue

            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=(
                        f"[lookup-documentation] Variable '{variable_name}' must "
                        "have a supported documentation-exclusion directive "
                        "immediately above it"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )

    def check_redundant_docker_layers(self) -> None:
        """Rule 13: Check empty Docker layers have another meaningful source."""
        role_name = self.file_path.parent.parent.name
        docker_prefix = f"{role_name}_role_docker_"
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        for default_name, (_, default_lines) in variables.items():
            if not default_name.startswith(docker_prefix) or not default_name.endswith(
                "_default"
            ):
                continue

            aggregate_name = default_name.removesuffix("_default")
            if aggregate_name.endswith("_docker_networks"):
                continue

            custom_name = f"{aggregate_name}_custom"
            if custom_name not in variables:
                continue

            default_value = re.fullmatch(
                re.escape(default_name) + r":\s*(\{\}|\[\])", default_lines[0]
            )
            _, custom_lines = variables[custom_name]
            custom_value = re.fullmatch(
                re.escape(custom_name) + r":\s*(\{\}|\[\])", custom_lines[0]
            )
            if (
                default_value is None
                or custom_value is None
                or default_value.group(1) != custom_value.group(1)
            ):
                continue

            if aggregate_name not in variables:
                line_number, _ = variables[default_name]
                is_redundant = True
            else:
                line_number, aggregate_lines = variables[aggregate_name]
                expression = self.variable_jinja_expression(
                    line_number, aggregate_lines
                )
                lookup_base = aggregate_name.removeprefix(f"{role_name}_role")
                default_lookup = self.explicit_role_var_lookup_pattern(
                    f"{lookup_base}_default", role_name
                ).search(expression)
                custom_lookup = self.explicit_role_var_lookup_pattern(
                    f"{lookup_base}_custom", role_name
                ).search(expression)
                if default_lookup is None or custom_lookup is None:
                    continue

                prefix = expression[: default_lookup.start()]
                between_lookups = expression[
                    default_lookup.end() : custom_lookup.start()
                ]
                suffix = expression[custom_lookup.end() :]
                has_direct_prefix = re.fullmatch(
                    "^" + re.escape(aggregate_name) + r":\s*['\"]\{\{\s*",
                    prefix,
                )
                if default_value.group(1) == "[]":
                    is_redundant = (
                        has_direct_prefix is not None
                        and re.fullmatch(r"\s*\+\s*", between_lookups) is not None
                        and re.fullmatch(r"\s*\}\}['\"]\s*", suffix) is not None
                    )
                else:
                    is_redundant = (
                        has_direct_prefix is not None
                        and re.fullmatch(
                            r"\s*\|\s*combine\(\s*", between_lookups
                        )
                        is not None
                        and re.fullmatch(r"\s*\)\s*\}\}['\"]\s*", suffix)
                        is not None
                    )

            if not is_redundant:
                continue

            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=(
                        f"[redundant-docker-layers] Variable '{aggregate_name}' "
                        "only combines empty default and custom layers; omit the "
                        "unused Docker section"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )

    def check_docker_hosts_formula(self) -> None:
        """Rule 14: Check Docker hosts use only role-local mapping layers."""
        role_name = self.file_path.parent.parent.name
        hosts_name = f"{role_name}_role_docker_hosts"
        hosts_default_name = f"{hosts_name}_default"
        hosts_custom_name = f"{hosts_name}_custom"
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }
        if hosts_name not in variables:
            return

        line_number, hosts_lines = variables[hosts_name]
        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        missing_defaults = [
            variable_name
            for variable_name in (hosts_default_name, hosts_custom_name)
            if variable_name not in variables
        ]
        if missing_defaults:
            self.errors.append(
                LintError(
                    file=relative_path,
                    line=line_number,
                    message=(
                        f"[docker-hosts-formula] Variable '{hosts_name}' must "
                        f"define companion default(s): {', '.join(missing_defaults)}"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )
            return

        expression = self.variable_jinja_expression(line_number, hosts_lines)
        default_lookup = self.explicit_role_var_lookup_pattern(
            "_docker_hosts_default", role_name
        ).search(expression)
        custom_lookup = self.explicit_role_var_lookup_pattern(
            "_docker_hosts_custom", role_name
        ).search(expression)
        if default_lookup is None or custom_lookup is None:
            return

        prefix = expression[: default_lookup.start()]
        between_lookups = expression[default_lookup.end() : custom_lookup.start()]
        suffix = expression[custom_lookup.end() :]
        is_supported = (
            re.fullmatch(
                "^" + re.escape(hosts_name) + r":\s*['\"]\{\{\s*", prefix
            )
            is not None
            and re.fullmatch(r"\s*\|\s*combine\(\s*", between_lookups) is not None
            and re.fullmatch(r"\s*\)\s*\}\}['\"]\s*", suffix) is not None
        )
        if is_supported:
            return

        self.errors.append(
            LintError(
                file=relative_path,
                line=line_number,
                message=(
                    f"[docker-hosts-formula] Variable '{hosts_name}' must only "
                    "combine its role-local default and custom host mappings"
                ),
                repo_url=self.repo_url,
                commit_sha=self.commit_sha,
            )
        )

    def check_traefik_api_router_contract(self) -> None:
        """Rule 15: Check the complete Traefik API router contract."""
        role_name = self.file_path.parent.parent.name
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        traefik_enabled_name = f"{role_name}_role_traefik_enabled"
        api_enabled_name = f"{role_name}_role_traefik_api_enabled"
        api_endpoint_name = f"{role_name}_role_traefik_api_endpoint"
        default_name = f"{role_name}_role_traefik_middleware_default_api"
        custom_name = f"{role_name}_role_traefik_middleware_custom_api"
        legacy_names = (
            f"{role_name}_role_traefik_api_middleware",
            f"{role_name}_role_traefik_middleware_api",
        )

        if traefik_enabled_name in variables:
            missing_names = [
                variable_name
                for variable_name in (
                    default_name,
                    custom_name,
                    api_enabled_name,
                    api_endpoint_name,
                )
                if variable_name not in variables
            ]
            if missing_names:
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=variables[traefik_enabled_name][0],
                        message=(
                            "[traefik-api-router-contract] Traefik roles must "
                            f"define {', '.join(missing_names)}"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )
            elif variables[default_name][0] > variables[custom_name][0]:
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=variables[custom_name][0],
                        message=(
                            "[traefik-api-router-contract] "
                            f"'{default_name}' must be declared before '{custom_name}'"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )

        for legacy_name in legacy_names:
            if legacy_name not in variables:
                continue
            self.errors.append(
                LintError(
                    file=relative_path,
                    line=variables[legacy_name][0],
                    message=(
                        "[traefik-api-router-contract] Legacy secure API middleware "
                        f"variable '{legacy_name}' is not supported; use the default/custom API pair"
                    ),
                    repo_url=self.repo_url,
                    commit_sha=self.commit_sha,
                )
            )

    def check_nested_traefik_adapter_contract(self) -> None:
        """Rule 16: Check namespaced API contracts forwarded to included roles."""
        role_name = self.file_path.parent.parent.name
        role_path = self.file_path.parent.parent
        task_files = sorted((role_path / "tasks").glob("**/*.yml"))
        if not task_files:
            return

        tasks_source = "\n".join(
            task_file.read_text(encoding="utf-8") for task_file in task_files
        )
        adapter_pattern = re.compile(
            rf"^\s+([a-z][a-z0-9_]*)_role_web_subdomain:\s+.*"
            rf"lookup\(\s*(['\"])role_var\2\s*,\s*(['\"])_([a-z][a-z0-9_]*)_web_subdomain\3"
            rf"\s*,\s*role\s*=\s*(['\"]){re.escape(role_name)}\5\s*\)",
            flags=re.MULTILINE,
        )
        adapters = {
            match.group(1)
            for match in adapter_pattern.finditer(tasks_source)
            if match.group(1) == match.group(4)
        }
        if not adapters:
            return

        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }
        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)

        contract_suffixes = (
            "traefik_sso_middleware",
            "traefik_middleware_default",
            "traefik_middleware_custom",
            "traefik_middleware_default_api",
            "traefik_middleware_custom_api",
            "traefik_certresolver",
            "traefik_enabled",
            "traefik_api_enabled",
            "traefik_api_endpoint",
        )
        for adapter in sorted(adapters):
            anchor_name = f"{role_name}_role_{adapter}_web_subdomain"
            anchor_line = variables.get(anchor_name, (1, []))[0]
            missing_defaults = [
                f"{role_name}_role_{adapter}_{suffix}"
                for suffix in contract_suffixes
                if f"{role_name}_role_{adapter}_{suffix}" not in variables
            ]
            if missing_defaults:
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=anchor_line,
                        message=(
                            "[nested-traefik-adapter-contract] Namespaced web adapter "
                            f"'{adapter}' must define {', '.join(missing_defaults)}"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )

            missing_forwarding = []
            for suffix in contract_suffixes:
                target_name = f"{adapter}_role_{suffix}:"
                lookup_suffix = f"_{adapter}_{suffix}"
                if target_name not in tasks_source or lookup_suffix not in tasks_source:
                    missing_forwarding.append(target_name.removesuffix(":"))
            if missing_forwarding:
                self.errors.append(
                    LintError(
                        file=relative_path,
                        line=anchor_line,
                        message=(
                            "[nested-traefik-adapter-contract] Namespaced web adapter "
                            f"'{adapter}' must forward {', '.join(missing_forwarding)}"
                        ),
                        repo_url=self.repo_url,
                        commit_sha=self.commit_sha,
                    )
                )

    def check_docker_helper_var_prefix(self) -> None:
        """Rule 17: Check lifecycle helper callers use var_prefix."""
        role_path = self.file_path.parent.parent
        helper_pattern = re.compile(
            r"/docker/(?:create|remove|restart|start|stop)_docker_container\.yml"
        )

        for task_file in sorted((role_path / "tasks").glob("**/*.yml")):
            task_lines = task_file.read_text(encoding="utf-8").splitlines()
            for line_index, line in enumerate(task_lines):
                if helper_pattern.search(line) is None:
                    continue
                for argument_index in range(
                    line_index + 1, min(line_index + 9, len(task_lines))
                ):
                    argument_line = task_lines[argument_index]
                    if argument_line.startswith("- name:"):
                        break
                    if re.match(r"^\s+_var_prefix:\s*", argument_line) is None:
                        continue
                    try:
                        relative_path = str(task_file.relative_to(Path.cwd()))
                    except ValueError:
                        relative_path = str(task_file)
                    self.errors.append(
                        LintError(
                            file=relative_path,
                            line=argument_index + 1,
                            message=(
                                "[docker-helper-prefix-argument] Docker lifecycle "
                                "helpers accept 'var_prefix', not '_var_prefix'"
                            ),
                            repo_url=self.repo_url,
                            commit_sha=self.commit_sha,
                        )
                    )

    def check_non_docker_traefik_renderer_contract(self) -> None:
        """Rule 18: Check non-Docker renderers consume the API contract."""
        role_name = self.file_path.parent.parent.name
        role_path = self.file_path.parent.parent
        variables = {
            variable_name: (line_number, variable_lines)
            for variable_name, line_number, variable_lines in self.iter_top_level_variables()
        }
        traefik_enabled_name = f"{role_name}_role_traefik_enabled"
        if traefik_enabled_name not in variables:
            return

        source_files = sorted((role_path / "tasks").glob("**/*.yml")) + sorted(
            (role_path / "templates").glob("**/*.j2")
        )
        renderer_source = "\n".join(
            source_file.read_text(encoding="utf-8") for source_file in source_files
        )
        if "create_docker_container.yml" in renderer_source:
            return
        if "deprecated" in renderer_source:
            return
        if "docker_labels_common" in renderer_source:
            return

        required_tokens = (
            "traefik_middleware_api",
            "_traefik_api_enabled",
            "_traefik_api_endpoint",
        )
        missing_tokens = [
            token for token in required_tokens if token not in renderer_source
        ]
        if not missing_tokens:
            return

        try:
            relative_path = str(self.file_path.relative_to(Path.cwd()))
        except ValueError:
            relative_path = str(self.file_path)
        self.errors.append(
            LintError(
                file=relative_path,
                line=variables[traefik_enabled_name][0],
                message=(
                    "[non-docker-traefik-renderer-contract] Traefik role without "
                    "shared Docker labels must render "
                    f"{', '.join(missing_tokens)}"
                ),
                repo_url=self.repo_url,
                commit_sha=self.commit_sha,
            )
        )

    def variable_jinja_expression(
        self, line_number: int, variable_lines: list[str]
    ) -> str:
        """Return the first complete Jinja expression for a top-level variable."""
        for jinja_start_line, _, jinja_lines in self.iter_multiline_jinja_blocks():
            if jinja_start_line == line_number:
                return "\n".join(jinja_lines)
        return variable_lines[0]

    @staticmethod
    def iter_lookup_calls(source: str) -> Iterator[tuple[int, str]]:
        """Yield balanced lookup() calls while respecting quoted content."""
        search_column = 0
        while match := re.search(r"\blookup\s*\(", source[search_column:]):
            call_start = search_column + match.start()
            open_parenthesis = source.find("(", call_start)
            depth = 0
            quote: str | None = None
            escaped = False
            call_end: int | None = None

            for column in range(open_parenthesis, len(source)):
                char = source[column]
                if escaped:
                    escaped = False
                elif char == "\\" and quote is not None:
                    escaped = True
                elif quote is not None:
                    if char == quote:
                        quote = None
                elif char in ("'", '"'):
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        call_end = column + 1
                        break

            if call_end is None:
                return

            yield call_start, source[call_start:call_end]
            search_column = call_end

    @staticmethod
    def call_has_keyword_argument(call: str, keyword: str) -> bool:
        """Return whether a function call has a top-level keyword argument."""
        depth = 0
        quote: str | None = None
        escaped = False
        column = 0

        while column < len(call):
            char = call[column]
            if escaped:
                escaped = False
            elif char == "\\" and quote is not None:
                escaped = True
            elif quote is not None:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 1 and call.startswith(keyword, column):
                before = call[column - 1] if column > 0 else ""
                after_keyword = column + len(keyword)
                after = call[after_keyword] if after_keyword < len(call) else ""
                if not (before.isalnum() or before == "_") and not (
                    after.isalnum() or after == "_"
                ):
                    equals_column = after_keyword
                    while (
                        equals_column < len(call)
                        and call[equals_column].isspace()
                    ):
                        equals_column += 1
                    if equals_column < len(call) and call[equals_column] == "=":
                        return True

            column += 1

        return False

    @staticmethod
    def explicit_role_var_lookup_pattern(suffix: str, role_name: str) -> re.Pattern[str]:
        """Return a pattern for the repository's explicit role_var lookup form."""
        return re.compile(
            rf"lookup\(\s*(['\"])role_var\1\s*,\s*(['\"]){re.escape(suffix)}\2"
            rf"\s*,\s*role\s*=\s*(['\"]){re.escape(role_name)}\3\s*\)"
        )

    def iter_top_level_variables(self) -> Iterator[TopLevelVariable]:
        """Yield each top-level defaults variable and all lines in its YAML block."""
        variable_name: str | None = None
        start_line: int | None = None
        variable_lines: list[str] = []

        for line_number, line in enumerate(self.lines, 1):
            match = re.match(r"^([a-z][a-z0-9_]*):(?:\s|$)", line)
            if match:
                if variable_name is not None and start_line is not None:
                    yield variable_name, start_line, variable_lines
                variable_name = match.group(1)
                start_line = line_number
                variable_lines = [line]
            elif variable_name is not None:
                variable_lines.append(line)

        if variable_name is not None and start_line is not None:
            yield variable_name, start_line, variable_lines

    def iter_multiline_jinja_blocks(self) -> Iterator[JinjaBlock]:
        """Yield multiline Jinja blocks without confusing mapping braces for }}."""
        start_line: int | None = None
        bracket_indent: int | None = None
        block_lines: list[str] = []
        quote: str | None = None
        escaped = False
        mapping_depth = 0

        for line_number, line in enumerate(self.lines, 1):
            search_column = 0
            line_added = False

            while search_column < len(line):
                if start_line is None:
                    open_column = line.find("{{", search_column)
                    if open_column == -1:
                        break

                    start_line = line_number
                    bracket_indent = open_column
                    block_lines = [line]
                    quote = None
                    escaped = False
                    mapping_depth = 0
                    search_column = open_column + len("{{")
                    line_added = True
                elif not line_added:
                    block_lines.append(line)
                    line_added = True

                close_column: int | None = None
                while search_column < len(line):
                    char = line[search_column]
                    if escaped:
                        escaped = False
                    elif char == "\\" and quote is not None:
                        escaped = True
                    elif quote is not None:
                        if char == quote:
                            quote = None
                    elif char in ("'", '"'):
                        quote = char
                    elif char == "{":
                        mapping_depth += 1
                    elif char == "}":
                        if mapping_depth > 0:
                            mapping_depth -= 1
                        elif line.startswith("}}", search_column):
                            close_column = search_column
                            break

                    search_column += 1

                if close_column is None:
                    break

                assert start_line is not None
                assert bracket_indent is not None
                if start_line != line_number:
                    yield start_line, bracket_indent, block_lines

                start_line = None
                bracket_indent = None
                block_lines = []
                quote = None
                escaped = False
                mapping_depth = 0
                search_column = close_column + len("}}")

    @staticmethod
    def update_parenthesis_contexts(
        line: str,
        parenthesis_contexts: list[ParenthesisContext],
        start_column: int = 0,
    ) -> None:
        """Update unmatched parenthesis contexts found in one expression line."""
        quote: str | None = None
        escaped = False

        for column in range(start_column, len(line)):
            char = line[column]
            if escaped:
                escaped = False
            elif char == "\\" and quote is not None:
                escaped = True
            elif quote is not None:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "(":
                previous_char = line[column - 1] if column > 0 else ""
                is_function_call = previous_char.isalnum() or previous_char in "_])"
                if is_function_call:
                    parenthesis_contexts.append((column + 1, "function content"))
                else:
                    parenthesis_contexts.append((column + 1, "grouping content"))
            elif char == ")" and parenthesis_contexts:
                _ = parenthesis_contexts.pop()

    def check_ifelse_alignment(self) -> None:
        """
        Rule 2: Check if/else keywords align with their expression context

        Example:
            variable: "{{ value
                       if condition
                       else other_value }}"
                       ^ if and else must align with the opening {{ brackets

        Nested conditionals align with their innermost unmatched opening parenthesis:
            variable: "{{ value
                          + (nested_value
                             if condition
                             else fallback) }}"
                             ^ Aligns with the opening parenthesis

        Conditionals inside function calls align with the function content:
            variable: "{{ value
                          | function(nested_value
                                     if condition
                                     else fallback) }}"
                                     ^ Aligns one column after the opening parenthesis
        """
        for (
            jinja_start_line,
            jinja_bracket_indent,
            jinja_lines,
        ) in self.iter_multiline_jinja_blocks():
            parenthesis_contexts: list[ParenthesisContext] = []
            self.update_parenthesis_contexts(
                jinja_lines[0], parenthesis_contexts, jinja_bracket_indent + len("{{")
            )
            try:
                relative_path = str(self.file_path.relative_to(Path.cwd()))
            except ValueError:
                relative_path = str(self.file_path)

            for line_offset, continuation_line in enumerate(jinja_lines[1:], 1):
                stripped_line = continuation_line.strip()
                expected_indent = (
                    parenthesis_contexts[-1][0]
                    if parenthesis_contexts
                    else jinja_bracket_indent
                )
                context = parenthesis_contexts[-1][1] if parenthesis_contexts else "{{"

                # Inline conditionals do not have a continuation-line
                # alignment requirement.
                keyword: str | None = None
                if stripped_line.startswith("if "):
                    keyword = "if"
                elif stripped_line.startswith("else "):
                    keyword = "else"

                if keyword is not None:
                    actual_indent = len(continuation_line) - len(
                        continuation_line.lstrip()
                    )
                    if actual_indent != expected_indent:
                        diff = actual_indent - expected_indent
                        self.errors.append(
                            LintError(
                                file=relative_path,
                                line=jinja_start_line + line_offset,
                                message=f"[ifelse-alignment] '{keyword}' at column {actual_indent} doesn't align with '{context}' at column {expected_indent} (off by {diff:+d})",
                                repo_url=self.repo_url,
                                commit_sha=self.commit_sha,
                            )
                        )

                self.update_parenthesis_contexts(
                    continuation_line, parenthesis_contexts
                )

    def lint(self) -> list[LintError]:
        """Run all lint checks and return list of errors"""
        self.check_operator_alignment()
        self.check_ifelse_alignment()
        self.check_variable_prefix()
        self.check_docker_layer_composition()
        self.check_web_url_composition()
        self.check_web_url_literal_prefix()
        self.check_docker_image_composition()
        self.check_explicit_role_var_target()
        self.check_docker_network_formula()
        self.check_section_structure()
        self.check_docker_envs_custom_usage()
        self.check_lookup_documentation()
        self.check_redundant_docker_layers()
        self.check_docker_hosts_formula()
        self.check_traefik_api_router_contract()
        self.check_nested_traefik_adapter_contract()
        self.check_docker_helper_var_prefix()
        self.check_non_docker_traefik_renderer_contract()
        return self.errors


def markdown_cell(value: str) -> str:
    """Escape content for use in a GitHub Markdown table cell."""
    return value.replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def write_github_summary(errors: list[LintError], files_checked: int) -> None:
    """Append a defaults-lint report to the GitHub Actions job summary."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    passed = not errors
    result = "✅ Passed" if passed else "❌ Failed"
    lines = [
        "## Defaults lint report",
        "",
        "| Result | Files checked | Findings |",
        "| --- | ---: | ---: |",
        f"| {result} | {files_checked} | {len(errors)} |",
    ]

    if errors:
        lines.extend(
            [
                "",
                "### Findings",
                "",
                "| File | Line | Finding |",
                "| --- | ---: | --- |",
            ]
        )
        for error in errors:
            file_name = markdown_cell(error.file)
            github_link = error.github_link()
            file_cell = f"[{file_name}]({github_link})" if github_link else file_name
            lines.append(
                f"| {file_cell} | {error.line} | {markdown_cell(error.message)} |"
            )

    with Path(summary_path).open("a", encoding="utf-8") as summary_file:
        _ = summary_file.write("\n".join(lines) + "\n")


def main() -> None:
    """Main entry point for the linter"""
    if len(sys.argv) < 2:
        print("Usage: python3 saltbox-defaults-linter.py <roles_directory>")
        print("\nExample:")
        print("  python3 saltbox-defaults-linter.py roles/")
        sys.exit(1)

    roles_dir = Path(sys.argv[1])

    if not roles_dir.exists():
        print(f"Error: Directory '{roles_dir}' does not exist")
        sys.exit(1)

    if not roles_dir.is_dir():
        print(f"Error: '{roles_dir}' is not a directory")
        sys.exit(1)

    # Get GitHub repo and commit info from environment variables (set by GitHub Actions)
    # GITHUB_REPOSITORY format: "owner/repo"
    # GITHUB_SHA: commit SHA that triggered the workflow
    github_repo = os.environ.get("GITHUB_REPOSITORY")
    github_sha = os.environ.get("GITHUB_SHA")

    # Build full repo URL if we have the repository name
    repo_url = f"https://github.com/{github_repo}" if github_repo else None

    all_errors: list[LintError] = []
    files_checked = 0

    # Find and lint all defaults/main.yml files
    for defaults_file in sorted(roles_dir.glob("*/defaults/main.yml")):
        linter = DefaultsLinter(defaults_file, repo_url=repo_url, commit_sha=github_sha)
        errors = linter.lint()
        all_errors.extend(errors)
        files_checked += 1

    # Output results
    if all_errors:
        print(
            f"❌ Found {len(all_errors)} formatting error(s) in {files_checked} file(s):\n"
        )
        for error in all_errors:
            print(error.to_github_annotation())
        print(f"\nTotal: {len(all_errors)} error(s)")
        write_github_summary(all_errors, files_checked)
        sys.exit(1)
    else:
        print(f"✅ All {files_checked} role defaults files pass formatting checks")
        write_github_summary(all_errors, files_checked)
        sys.exit(0)


if __name__ == "__main__":
    main()
