from __future__ import annotations

# pyright: reportMissingImports=false, reportUnknownVariableType=false
import shlex
from collections.abc import Callable, Sequence

from ansible.errors import AnsibleFilterError  # type: ignore[import-not-found]


def saltbox_argv(value: object, description: str = "argument value") -> list[str]:
    """Normalize a legacy shell-word string or an argv list to a string list."""
    if isinstance(value, str):
        try:
            return shlex.split(value, posix=True)
        except ValueError as exc:
            raise AnsibleFilterError(
                f"{description} contains invalid shell quoting: {exc}"
            ) from exc

    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise AnsibleFilterError(f"{description} must be a string or list of strings")
    arguments: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AnsibleFilterError(
                f"{description} must be a string or list of strings"
            )
        arguments.append(item)
    return arguments


class FilterModule:
    def filters(self) -> dict[str, Callable[..., list[str]]]:
        return {"saltbox_argv": saltbox_argv}
