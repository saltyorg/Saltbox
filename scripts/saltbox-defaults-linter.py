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
"""

import re
import sys
from pathlib import Path
from typing import List


class LintError:
    """Represents a single linting error"""

    def __init__(self, file: str, line: int, message: str, repo_url: str = None, commit_sha: str = None):
        self.file = file
        self.line = line
        self.message = message
        self.repo_url = repo_url
        self.commit_sha = commit_sha

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
            github_link = f"{self.repo_url}/blob/{self.commit_sha}/{self.file}#L{self.line}"
            message_with_link = f"{self.message} - {github_link}"
        else:
            message_with_link = self.message

        return f"::error file={self.file},line={self.line}::{message_with_link}"

    def __str__(self) -> str:
        return f"{self.file}:{self.line} - {self.message}"


class DefaultsLinter:
    """Lints a single defaults/main.yml file"""

    def __init__(self, file_path: Path, repo_url: str = None, commit_sha: str = None):
        self.file_path = file_path
        self.repo_url = repo_url
        self.commit_sha = commit_sha
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

                            # Get relative path for GitHub annotation
                            try:
                                relative_path = str(self.file_path.relative_to(Path.cwd()))
                            except ValueError:
                                relative_path = str(self.file_path)

                            self.errors.append(LintError(
                                file=relative_path,
                                line=j + 1,  # Convert to 1-indexed
                                message=f"[operator-alignment] Variable '{var_name}': Operator '{operator}' at column {actual_spaces}, expected {expected_spaces} (off by {diff:+d})",
                                repo_url=self.repo_url,
                                commit_sha=self.commit_sha
                            ))

                    # Stop if we've reached the end of the Jinja block
                    if '}}' in continuation_line:
                        break

                    j += 1

                # Skip ahead past the multi-line block we just processed
                i = j

            i += 1

    def check_variable_prefix(self):
        """Rule 3: Check top-level role defaults use the role's prefix."""
        role_name = self.file_path.parent.parent.name
        expected_prefix = f"{role_name}_"

        for line_number, line in enumerate(self.lines, 1):
            match = re.match(r'^([a-z][a-z0-9_]*):(?:\s|$)', line)
            if not match:
                continue

            variable_name = match.group(1)
            if '_role_' not in variable_name:
                continue

            if variable_name.startswith(expected_prefix):
                continue

            try:
                relative_path = str(self.file_path.relative_to(Path.cwd()))
            except ValueError:
                relative_path = str(self.file_path)

            self.errors.append(LintError(
                file=relative_path,
                line=line_number,
                message=f"[variable-prefix] Variable '{variable_name}' must start with '{expected_prefix}'",
                repo_url=self.repo_url,
                commit_sha=self.commit_sha
            ))

    def iter_multiline_jinja_blocks(self):
        """Yield every multiline Jinja block, including blocks embedded in strings."""
        start_line = None
        bracket_indent = None
        block_lines = []

        for line_number, line in enumerate(self.lines, 1):
            search_column = 0

            while search_column < len(line):
                if start_line is None:
                    open_column = line.find('{{', search_column)
                    if open_column == -1:
                        break

                    close_column = line.find('}}', open_column + 2)
                    if close_column != -1:
                        search_column = close_column + 2
                        continue

                    start_line = line_number
                    bracket_indent = open_column
                    block_lines = [line]
                    break

                close_column = line.find('}}', search_column)
                if close_column == -1:
                    block_lines.append(line)
                    break

                block_lines.append(line)
                yield start_line, bracket_indent, block_lines

                start_line = None
                bracket_indent = None
                block_lines = []
                search_column = close_column + 2

    @staticmethod
    def update_parenthesis_contexts(line, parenthesis_contexts, start_column=0):
        """Update unmatched parenthesis contexts found in one expression line."""
        quote = None
        escaped = False

        for column in range(start_column, len(line)):
            char = line[column]
            if escaped:
                escaped = False
            elif char == '\\' and quote is not None:
                escaped = True
            elif quote is not None:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == '(':
                previous_char = line[column - 1] if column > 0 else ''
                is_function_call = (previous_char.isalnum()
                                    or previous_char in '_])')
                if is_function_call:
                    parenthesis_contexts.append((column + 1, 'function content'))
                else:
                    parenthesis_contexts.append((column + 1, 'grouping content'))
            elif char == ')' and parenthesis_contexts:
                parenthesis_contexts.pop()

    def check_ifelse_alignment(self):
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
        for (jinja_start_line,
             jinja_bracket_indent,
             jinja_lines) in self.iter_multiline_jinja_blocks():
            parenthesis_contexts = []
            self.update_parenthesis_contexts(
                jinja_lines[0],
                parenthesis_contexts,
                jinja_bracket_indent + len('{{')
            )
            try:
                relative_path = str(self.file_path.relative_to(Path.cwd()))
            except ValueError:
                relative_path = str(self.file_path)

            for line_offset, continuation_line in enumerate(jinja_lines[1:], 1):
                stripped_line = continuation_line.strip()
                expected_indent = (parenthesis_contexts[-1][0]
                                   if parenthesis_contexts
                                   else jinja_bracket_indent)
                context = (parenthesis_contexts[-1][1]
                           if parenthesis_contexts
                           else '{{')

                # Inline conditionals do not have a continuation-line
                # alignment requirement.
                keyword = None
                if stripped_line.startswith('if '):
                    keyword = 'if'
                elif stripped_line.startswith('else '):
                    keyword = 'else'

                if keyword is not None:
                    actual_indent = len(continuation_line) - len(continuation_line.lstrip())
                    if actual_indent != expected_indent:
                        diff = actual_indent - expected_indent
                        self.errors.append(LintError(
                            file=relative_path,
                            line=jinja_start_line + line_offset,
                            message=f"[ifelse-alignment] '{keyword}' at column {actual_indent} doesn't align with '{context}' at column {expected_indent} (off by {diff:+d})",
                            repo_url=self.repo_url,
                            commit_sha=self.commit_sha
                        ))

                self.update_parenthesis_contexts(
                    continuation_line,
                    parenthesis_contexts
                )

    def lint(self) -> List[LintError]:
        """Run all lint checks and return list of errors"""
        self.check_operator_alignment()
        self.check_ifelse_alignment()
        self.check_variable_prefix()
        return self.errors


def main():
    """Main entry point for the linter"""
    import os

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
    github_repo = os.environ.get('GITHUB_REPOSITORY')
    github_sha = os.environ.get('GITHUB_SHA')

    # Build full repo URL if we have the repository name
    repo_url = f"https://github.com/{github_repo}" if github_repo else None

    all_errors = []
    files_checked = 0

    # Find and lint all defaults/main.yml files
    for defaults_file in sorted(roles_dir.glob("*/defaults/main.yml")):
        linter = DefaultsLinter(defaults_file, repo_url=repo_url, commit_sha=github_sha)
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
