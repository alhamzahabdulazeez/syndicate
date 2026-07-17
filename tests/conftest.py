from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable

import pytest

from syndicate.runtime.base import ActionResult


class ScriptedRuntimeClient:
    """Deterministic RuntimeClient for driving executor/validator/escalation
    behavior without a network, Docker, or a live container.

    Each call to run_action consumes one queued ActionResult for the first
    matching command prefix (in the order queued for that prefix); once a
    prefix's queue is exhausted, later calls to it get a default success
    result. This is how tests script "fails once then succeeds" or "fails
    three times" sequences.
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._queues: dict[str, list[ActionResult]] = defaultdict(list)
        self.commands: list[str] = []

    def queue(self, prefix: str, *results: ActionResult) -> None:
        self._queues[prefix].extend(results)

    async def run_action(
        self, action: str, cwd: str | None = None, timeout: float | None = None
    ) -> ActionResult:
        self.commands.append(action)
        for prefix, queue in self._queues.items():
            if action.startswith(prefix) and queue:
                return queue.pop(0)
        return ActionResult(exit_code=0, output=f'[scripted] {action}')

    async def read_file(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    async def write_file(self, path: str, content: str) -> None:
        self._files[path] = content

    async def edit_file(self, path: str, old_string: str, new_string: str) -> None:
        content = await self.read_file(path)
        self._files[path] = content.replace(old_string, new_string, 1)

    async def aclose(self) -> None:
        return None


class LocalGitRuntimeClient:
    """Runs commands as real local subprocesses rooted at a caller-supplied
    cwd (always a tmp_path-backed directory in these tests). Used only for
    oversight_git_node tests, where the claim under test ("DONE only when
    HEAD actually moved") has to be checked against real git semantics, not
    a scripted stand-in that could just be told the "right" answer.

    git init/add/commit/rev-parse are all local-only -- no network egress.
    """

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run_action(
        self, action: str, cwd: str | None = None, timeout: float | None = None
    ) -> ActionResult:
        self.commands.append(action)
        proc = await asyncio.create_subprocess_shell(
            action,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        return ActionResult(
            exit_code=proc.returncode or 0, output=stdout.decode(errors='replace')
        )

    async def read_file(self, path: str) -> str:
        raise NotImplementedError

    async def write_file(self, path: str, content: str) -> None:
        raise NotImplementedError

    async def edit_file(self, path: str, old_string: str, new_string: str) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


class LyingCommitRuntimeClient(LocalGitRuntimeClient):
    """Same as LocalGitRuntimeClient, except `git commit` is faked: it
    reports exit_code=0 without ever running the real command. Models a
    runtime client (or a hook wrapper) that claims success without acting,
    so real HEAD genuinely never moves -- this is what the HEAD-before/after
    check in oversight_git_node exists to catch, as distinct from a commit
    that honestly reports its own failure.
    """

    async def run_action(
        self, action: str, cwd: str | None = None, timeout: float | None = None
    ) -> ActionResult:
        if action.startswith('git commit'):
            self.commands.append(action)
            return ActionResult(exit_code=0, output='[lying] pretending to commit')
        return await super().run_action(action, cwd=cwd, timeout=timeout)


@pytest.fixture
def install_runtime_client(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], Any]:
    """Installs `client` as syndicate.nodes.get_runtime_client()'s return
    value and hands it back so the test can inspect/script it further."""

    def _install(client: Any) -> Any:
        monkeypatch.setattr('syndicate.nodes.get_runtime_client', lambda: client)
        return client

    return _install


@pytest.fixture
def scripted_client(
    install_runtime_client: Callable[[Any], Any],
) -> ScriptedRuntimeClient:
    return install_runtime_client(ScriptedRuntimeClient())
