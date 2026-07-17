"""The execution-backend boundary: everything in syndicate/nodes.py drives a
sandbox exclusively through this interface, never through a specific
backend's wire protocol. `syndicate.runtime.openhands.OpenHandsRuntimeClient`
is today's only real adapter (talking to an external
`ghcr.io/openhands/agent-server` container over HTTP); `syndicate.runtime.mock`
provides a no-network stand-in for local dev/tests. Swapping execution
backends means adding a new module here that satisfies `RuntimeClient` --
no changes to syndicate/nodes.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ActionResult:
    exit_code: int
    output: str


def str_replace(content: str, old_string: str, new_string: str, path: str) -> str:
    count = content.count(old_string)
    if count != 1:
        raise ValueError(
            f'expected exactly one occurrence of old_string in {path!r}, found {count}'
        )
    return content.replace(old_string, new_string, 1)


@runtime_checkable
class RuntimeClient(Protocol):
    """Adapter interface a sandbox execution backend must satisfy."""

    async def run_action(
        self, action: str, cwd: str | None = None, timeout: float | None = None
    ) -> ActionResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def edit_file(self, path: str, old_string: str, new_string: str) -> None: ...

    async def aclose(self) -> None:
        """Release any held resources."""
        ...
