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

2. If/Else Alignment: if and else keywords must align vertically within {{ }} brackets
"""

import re
import sys
from pathlib import Path
from typing import List


class LintError:
    """Represents a single linting error"""

    def __init__(self, file: str, line: int, message: str):
        self.file = file
        self.line = line
        self.message = message

    def to_github_annotation(self) -> str:
        """Format error as GitHub Actions annotation"""
        return f"::error file={self.file},line={self.line}::{self.message}"

    def __str__(self) -> str:
        return f"{self.file}:{self.line} - {self.message}"


class DefaultsLinter:
    """Lints a single defaults/main.yml file"""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.lines = file_path.read_text().splitlines()
        self.errors: List[LintError] = []

    def check_operator_alignment(self):
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
        i = 0
        while i < len(self.lines):
            curr_line = self.lines[i]

            # Match variable definitions starting multi-line Jinja expressions
            # Pattern: variable_name: "{{ <content>
            match = re.match(r'^([a-z_]+): "{{ (.+)', curr_line)

            # Only process if:
            # 1. Line matches pattern
            # 2. Line doesn't end with }} (multi-line expression)
            if match and '}}' not in curr_line:
                var_name = match.group(1)

                # This is the start of a multi-line expression
                # Track the expected alignment position for continuation lines
                expected_alignment = len(curr_line.split('"{{')[0] + '"{{ ')

                # Now check all continuation lines until we hit }}
                j = i + 1
                # Track alignment context - changes when we encounter 'else' with content
                # and resets when if/else blocks close
                current_alignment = expected_alignment
                in_else_context = False

                while j < len(self.lines):
                    continuation_line = self.lines[j]

                    # Check if this line closes an if/else block (ends with )) or }))
                    # This resets alignment context back to base
                    if in_else_context and re.search(r'\)\)(?:\)|$)', continuation_line.rstrip()):
                        current_alignment = expected_alignment
                        in_else_context = False

                    # Check if this line starts with 'else' followed by content that continues
                    # This creates a new alignment context for subsequent operators
                    # Pattern: else followed by lookup/variable (not just closing like '])')
                    # BUT: only if the else block doesn't close on the same line
                    else_match = re.match(r'^(\s+)else (lookup|[a-z_]+)', continuation_line)
                    if else_match:
                        # Check if this line also closes the if/else block (contains )))
                        # If so, don't set a persistent context
                        closes_immediately = re.search(r'\)\)\)', continuation_line)

                        if not closes_immediately:
                            # New alignment context: content after 'else '
                            # Only persists if the line doesn't close the block
                            current_alignment = len(else_match.group(1)) + len('else ')
                            in_else_context = True

                    # Check if this line has an operator
                    op_match = re.match(r'^(\s+)([|+]) ', continuation_line)

                    if op_match:
                        actual_spaces = len(op_match.group(1))
                        operator = op_match.group(2)

                        # Use current alignment context (changes after 'else', resets after closing parens)
                        expected_spaces = current_alignment

                        if actual_spaces != expected_spaces:
                            diff = actual_spaces - expected_spaces

                            self.errors.append(LintError(
                                file=str(self.file_path),
                                line=j + 1,  # Convert to 1-indexed
                                message=f"[operator-alignment] Variable '{var_name}': Operator '{operator}' at column {actual_spaces}, expected {expected_spaces} (off by {diff:+d})"
                            ))

                    # Stop if we've reached the end of the Jinja block
                    if '}}' in continuation_line:
                        break

                    j += 1

                # Skip ahead past the multi-line block we just processed
                i = j

            i += 1

    def check_ifelse_alignment(self):
        """
        Rule 2: Check if/else keywords align within same {{ }} brackets

        Example:
            variable: "{{ value
                       if condition
                       else other_value }}"
                       ^ if and else must align vertically
        """
        in_multiline_jinja = False
        jinja_lines = []
        jinja_start_line = 0

        for i, line in enumerate(self.lines, 1):
            # Detect start of multi-line Jinja expression
            if '"{{' in line and '}}' not in line:
                in_multiline_jinja = True
                jinja_lines = [line]
                jinja_start_line = i

            elif in_multiline_jinja:
                jinja_lines.append(line)

                # Check if we've reached the end of the Jinja block
                if '}}' in line:
                    # Find lines containing 'if' and 'else'
                    if_lines = [l for l in jinja_lines if ' if ' in l or l.strip().startswith('if ')]
                    else_lines = [l for l in jinja_lines if ' else ' in l or l.strip().startswith('else ')]

                    # Only check if both if and else exist in the same block
                    if if_lines and else_lines:
                        # Find indentation of 'if' and 'else' keywords
                        if_indent = None
                        else_indent = None

                        for l in jinja_lines:
                            if ' if ' in l:
                                # Calculate column position where 'if' appears
                                if_indent = len(l) - len(l.lstrip())
                            if ' else ' in l or l.strip().startswith('else '):
                                else_indent = len(l) - len(l.lstrip())

                        # Check if if and else align
                        if if_indent is not None and else_indent is not None:
                            if if_indent != else_indent:
                                diff = else_indent - if_indent
                                self.errors.append(LintError(
                                    file=str(self.file_path),
                                    line=jinja_start_line,
                                    message=f"[ifelse-alignment] 'if' at column {if_indent} doesn't align with 'else' at column {else_indent} (off by {diff:+d})"
                                ))

                    # Reset state
                    in_multiline_jinja = False
                    jinja_lines = []

    def lint(self) -> List[LintError]:
        """Run all lint checks and return list of errors"""
        self.check_operator_alignment()
        self.check_ifelse_alignment()
        return self.errors


def main():
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

    all_errors = []
    files_checked = 0

    # Find and lint all defaults/main.yml files
    for defaults_file in sorted(roles_dir.glob("*/defaults/main.yml")):
        linter = DefaultsLinter(defaults_file)
        errors = linter.lint()
        all_errors.extend(errors)
        files_checked += 1

    # Output results
    if all_errors:
        print(f"❌ Found {len(all_errors)} formatting error(s) in {files_checked} file(s):\n")
        for error in all_errors:
            print(error.to_github_annotation())
        print(f"\nTotal: {len(all_errors)} error(s)")
        sys.exit(1)
    else:
        print(f"✅ All {files_checked} role defaults files pass formatting checks")
        sys.exit(0)


if __name__ == "__main__":
    main()
