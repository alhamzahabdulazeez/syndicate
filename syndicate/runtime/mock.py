"""No-network stand-in for local dev/tests. Selected via
`SYNDICATE_MOCK_CLIENT=1` -- see `syndicate.runtime.factory.get_runtime_client`.
"""

from __future__ import annotations

from syndicate.runtime.base import ActionResult, str_replace


class MockRuntimeClient:
    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    async def run_action(
        self, action: str, cwd: str | None = None, timeout: float | None = None
    ) -> ActionResult:
        return ActionResult(exit_code=0, output=f'[mock] {action}')

    async def read_file(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def write_file(self, path: str, content: str) -> None:
        self._files[path] = content

    async def edit_file(self, path: str, old_string: str, new_string: str) -> None:
        self._files[path] = str_replace(
            await self.read_file(path), old_string, new_string, path
        )

    async def aclose(self) -> None:
        return None
