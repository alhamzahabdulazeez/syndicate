"""Adapter for a live OpenHands agent server.

Isolates every REST call to the external `ghcr.io/openhands/agent-server`
Docker container (see scripts/run_runtime_container.sh) behind the
`RuntimeClient` interface. Speaks the `openhands-agent-server` wire protocol
directly: actions run via `POST /api/bash/execute_bash_command`
(ExecuteBashRequest -> BashOutput), authenticated with the
`X-Session-API-Key` header used by that server's session-key auth
dependency. No other module talks to this container's HTTP API.
"""

from __future__ import annotations

from types import TracebackType

import httpx

from syndicate.runtime.base import ActionResult, str_replace


class OpenHandsRuntimeClient:
    def __init__(
        self,
        base_url: str,
        session_api_key: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        headers = {}
        if session_api_key:
            headers['X-Session-API-Key'] = session_api_key
        self._default_timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip('/'), headers=headers, timeout=timeout
        )

    async def run_action(
        self, action: str, cwd: str | None = None, timeout: float | None = None
    ) -> ActionResult:
        payload: dict[str, object] = {'command': action}
        if cwd is not None:
            payload['cwd'] = cwd
        if timeout is not None:
            payload['timeout'] = int(timeout)
        # execute_bash_command's own timeout field bounds the command; give
        # the HTTP call itself a little slack on top so we don't race it.
        request_timeout = (
            (timeout + 30) if timeout is not None else self._default_timeout
        )
        response = await self._client.post(
            '/api/bash/execute_bash_command',
            json=payload,
            timeout=request_timeout,
        )
        response.raise_for_status()
        data = response.json()

        exit_code = data.get('exit_code')
        if exit_code is None:
            raise RuntimeError(
                f'OpenHands runtime returned no exit_code for action {action!r}: '
                f'{data!r}'
            )
        output = (data.get('stdout') or '') + (data.get('stderr') or '')
        return ActionResult(exit_code=exit_code, output=output)

    async def read_file(self, path: str) -> str:
        response = await self._client.get(f'/api/file/download/{path}')
        if response.status_code == 404:
            raise FileNotFoundError(path)
        response.raise_for_status()
        return response.text

    async def write_file(self, path: str, content: str) -> None:
        filename = path.rsplit('/', 1)[-1] or 'file'
        response = await self._client.post(
            f'/api/file/upload/{path}',
            files={'file': (filename, content.encode())},
        )
        response.raise_for_status()

    async def edit_file(self, path: str, old_string: str, new_string: str) -> None:
        content = str_replace(await self.read_file(path), old_string, new_string, path)
        await self.write_file(path, content)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenHandsRuntimeClient:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
