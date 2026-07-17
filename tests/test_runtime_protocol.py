from __future__ import annotations

import dataclasses
import inspect

from syndicate.runtime.base import ActionResult, RuntimeClient
from syndicate.runtime.mock import MockRuntimeClient
from syndicate.runtime.openhands import OpenHandsRuntimeClient

_PROTOCOL_METHODS = ('run_action', 'read_file', 'write_file', 'edit_file', 'aclose')


def test_mock_runtime_client_satisfies_runtime_client_protocol():
    assert isinstance(MockRuntimeClient(), RuntimeClient)


def test_openhands_runtime_client_satisfies_runtime_client_protocol():
    # Construction only builds an httpx.AsyncClient (lazy connection pool);
    # no socket is opened and no request is made until a method is awaited,
    # which this test never does.
    client = OpenHandsRuntimeClient(base_url='http://localhost:1')
    assert isinstance(client, RuntimeClient)


def test_both_clients_expose_the_same_protocol_method_signatures():
    """Adding a backend means satisfying RuntimeClient -- proven here by
    comparing each concrete adapter's method signature against the
    Protocol's declared one, not just checking the method exists."""
    mock_client = MockRuntimeClient()
    openhands_client = OpenHandsRuntimeClient(base_url='http://localhost:1')

    for name in _PROTOCOL_METHODS:
        protocol_sig = inspect.signature(getattr(RuntimeClient, name))
        mock_sig = inspect.signature(getattr(mock_client, name))
        openhands_sig = inspect.signature(getattr(openhands_client, name))

        # Compare parameter names/kinds (drop `self` from the unbound
        # Protocol method; bound instance methods never carry it).
        protocol_params = list(protocol_sig.parameters)[1:]
        assert list(mock_sig.parameters) == protocol_params
        assert list(openhands_sig.parameters) == protocol_params


def test_action_result_shape_is_stable():
    fields = {f.name: f.type for f in dataclasses.fields(ActionResult)}
    assert fields == {'exit_code': 'int', 'output': 'str'}


async def test_mock_client_run_action_returns_an_action_result():
    result = await MockRuntimeClient().run_action('echo hi')
    assert isinstance(result, ActionResult)
    assert isinstance(result.exit_code, int)
    assert isinstance(result.output, str)
