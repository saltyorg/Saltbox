"""Filters used by the internal Hetzner GRUB remediation tasks."""

import re

from ansible.errors import AnsibleFilterError


_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*GRUB_CMDLINE_LINUX_DEFAULT[ \t]*=)"
    r"(?P<separator>[ \t]*)"
    r"(?P<right_hand_side>.*)$"
)
_TOKEN = re.compile(r"[^ \t]+")
_SHELL_CONTROL = re.compile(r"[;&|<>]")


def _without_nomodeset(value: str) -> tuple[str, bool]:
    changed = False

    def remove_token(match: re.Match[str]) -> str:
        nonlocal changed
        if match.group(0) == "nomodeset":
            changed = True
            return ""
        return match.group(0)

    return _TOKEN.sub(remove_token, value), changed


def _unsupported(line_number: int, explanation: str) -> AnsibleFilterError:
    return AnsibleFilterError(f"line {line_number}: {explanation}")


def _parse_value(right_hand_side: str, separator: str, line_number: int) -> tuple[str, str, str]:
    trailing_match = re.search(r"[ \t]*$", right_hand_side)
    trailing = trailing_match.group(0)
    expression = right_hand_side[:len(right_hand_side) - len(trailing)] if trailing else right_hand_side

    if expression.startswith(('"', "'")):
        quote = expression[0]
        if quote == '"' and "\\" in expression:
            raise _unsupported(line_number, "escape sequences are unsupported")
        closing_quote = expression.find(quote, 1)
        if closing_quote == -1:
            raise _unsupported(line_number, "unmatched quote")
        if closing_quote != len(expression) - 1:
            suffix = expression[closing_quote + 1:]
            if re.fullmatch(r"[ \t]+#.*", suffix):
                raise _unsupported(line_number, "inline comments are unsupported")
            raise _unsupported(line_number, "unsupported syntax after the quoted value")
        value = expression[1:-1]
        if quote == '"' and "`" in value:
            raise _unsupported(line_number, "command substitutions are unsupported")
        if quote == '"' and "$" in value:
            raise _unsupported(line_number, "expansions are unsupported")
        return quote, value, trailing

    if separator and expression.startswith("#"):
        raise _unsupported(line_number, "inline comments are unsupported")
    if re.search(r"[ \t]+#", expression):
        raise _unsupported(line_number, "inline comments are unsupported")
    if "\\" in expression:
        raise _unsupported(line_number, "escape sequences are unsupported")
    if "`" in expression:
        raise _unsupported(line_number, "command substitutions are unsupported")
    if "$" in expression or expression.startswith("~"):
        raise _unsupported(line_number, "expansions are unsupported")
    if _SHELL_CONTROL.search(expression):
        raise _unsupported(line_number, "unsupported syntax after the assignment value")
    if '"' in expression or "'" in expression:
        raise _unsupported(line_number, "unsupported quote in an unquoted value")
    if re.search(r"[ \t]", expression):
        raise _unsupported(line_number, "unquoted whitespace is unsupported")

    return "", expression, trailing


def hetzner_grub_without_nomodeset(content: object) -> dict[str, str | int]:
    """Remove exact ``nomodeset`` tokens from supported GRUB assignments."""
    if not isinstance(content, str):
        raise AnsibleFilterError("expected GRUB configuration text")

    transformed_lines = []
    assignment_count = 0
    changed_count = 0

    for line_number, line in enumerate(content.splitlines(keepends=True), start=1):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = _ASSIGNMENT.fullmatch(body)
        if match is None:
            transformed_lines.append(line)
            continue

        quote, value, trailing = _parse_value(
            match.group("right_hand_side"),
            match.group("separator"),
            line_number,
        )
        assignment_count += 1
        value, changed = _without_nomodeset(value)
        changed_count += int(changed)
        transformed_lines.append(
            f'{match.group("prefix")}{match.group("separator")}{quote}{value}{quote}{trailing}{ending}'
        )

    return {
        "content": "".join(transformed_lines),
        "assignment_count": assignment_count,
        "changed_count": changed_count,
    }


class FilterModule:
    """Expose Hetzner's private GRUB filter to Ansible."""

    def filters(self):
        return {
            "hetzner_grub_without_nomodeset": hetzner_grub_without_nomodeset,
        }
