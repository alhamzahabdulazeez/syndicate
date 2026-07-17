"""Scripted stub for the executor's one LLM-call seam (Step 7.5).

Drop-in stand-in for `anthropic.AsyncAnthropic()` as used in
`syndicate.nodes._run_agentic_loop`: identical call signature
(`.messages.create(model=, max_tokens=, tools=, messages=)`) and identical
response shape (`.content` -- a list of blocks with `.type` plus either
`.text` for a completion or `.name`/`.input`/`.id` for a tool_use -- and
`.stop_reason`) as the real binding. Selected via `SYNDICATE_STUB_LLM=1`,
mirroring how `syndicate.runtime.factory.get_runtime_client` uses
`SYNDICATE_MOCK_CLIENT=1`. No network, no key: only the brain is faked, the
muscle (real RuntimeClient / real container) is untouched.

Scripts are assigned per ticket id via `set_stub_script`, so a single test
run can give different tickets different scripted behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StubToolCall:
    name: str
    input: dict[str, Any]


@dataclass
class StubTurn:
    """One scripted LLM turn: either tool_use call(s), or a completion."""

    tool_calls: list[StubToolCall] = field(default_factory=list)
    done: bool = False
    text: str = 'done'


def tool_turn(*calls: tuple[str, dict[str, Any]]) -> StubTurn:
    """A turn issuing one or more tool_use blocks, e.g.

    tool_turn(("bash", {"command": "echo hi"}))
    tool_turn(("str_replace_based_edit_tool", {"command": "view", "path": "/x"}))
    """
    return StubTurn(tool_calls=[StubToolCall(name=n, input=i) for n, i in calls])


def complete_turn(text: str = 'done') -> StubTurn:
    """A turn signaling no more tool calls (stop_reason != "tool_use")."""
    return StubTurn(done=True, text=text)


class _Block:
    """Mimics an anthropic content block closely enough that the executor's
    existing block.type / block.name / block.input / block.id / block.text
    accesses work unmodified."""

    def __init__(self, type_: str, **kwargs: Any) -> None:
        self.type = type_
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, content: list[_Block], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, owner: 'StubAnthropicClient') -> None:
        self._owner = owner

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> _Response:
        # Recorded verbatim (not summarized) so tests can inspect exactly
        # what the executor handed the "brain" each turn -- this is the
        # wiring-level context-growth spot-check (Task 2.4). `messages` is
        # the same list object _run_agentic_loop keeps mutating turn over
        # turn, so it must be shallow-copied *now* -- otherwise every
        # recorded call would alias the same final, fully-grown list.
        self._owner.calls_seen.append(
            {
                'model': model,
                'max_tokens': max_tokens,
                'tools': tools,
                'messages': list(messages),
                'messages_len': len(messages),
            }
        )
        turn = self._owner.next_turn()
        if turn.done:
            return _Response(
                content=[_Block('text', text=turn.text)], stop_reason='end_turn'
            )

        call_index = self._owner.call_index
        blocks = [
            _Block(
                'tool_use',
                id=f'stub-{call_index}-{i}',
                name=call.name,
                input=call.input,
            )
            for i, call in enumerate(turn.tool_calls)
        ]
        return _Response(content=blocks, stop_reason='tool_use')


class StubAnthropicClient:
    """Same shape as anthropic.AsyncAnthropic(): `.messages.create(...)`."""

    def __init__(self, script: list[StubTurn]) -> None:
        if not script:
            raise ValueError('stub LLM script must have at least one turn')
        self._script = script
        self.call_index = 0
        self.calls_seen: list[dict[str, Any]] = []
        self.messages = _Messages(self)

    def next_turn(self) -> StubTurn:
        # Repeats the final scripted turn once exhausted, so a single
        # tool_turn(...) doubles as an "endless" script (Task 2.2's
        # inner-cap scenario) without needing an explicit infinite script.
        turn = self._script[min(self.call_index, len(self._script) - 1)]
        self.call_index += 1
        return turn


_scripts: dict[str, list[StubTurn]] = {}
_client_history: dict[str, list[StubAnthropicClient]] = {}


def set_stub_script(ticket_id: str, script: list[StubTurn]) -> None:
    _scripts[ticket_id] = script
    _client_history[ticket_id] = []


def get_stub_client(ticket_id: str) -> StubAnthropicClient:
    script = _scripts.get(ticket_id)
    if script is None:
        raise RuntimeError(
            f'no stub LLM script registered for ticket_id={ticket_id!r}; '
            'call syndicate.stub_llm.set_stub_script first'
        )
    client = StubAnthropicClient(script)
    _client_history.setdefault(ticket_id, []).append(client)
    return client


def get_call_history(ticket_id: str) -> list[StubAnthropicClient]:
    """One StubAnthropicClient per attempt/strike for this ticket, in order
    -- _run_agentic_loop constructs a fresh one each strike (mirroring the
    real code constructing a fresh anthropic.AsyncAnthropic() each strike),
    so this is how tests inspect strike-by-strike LLM traffic, e.g. to prove
    diagnosis-threading between strikes."""
    return list(_client_history.get(ticket_id, []))


def get_last_calls(ticket_id: str) -> list[dict[str, Any]]:
    """The `.messages.create()` call log from the most recent attempt/strike
    for this ticket -- what the executor actually handed the stub brain."""
    history = get_call_history(ticket_id)
    return history[-1].calls_seen if history else []
