from __future__ import annotations

from syndicate import nodes
from syndicate.state import DecisionSummary


def test_completed_ticket_appends_exactly_one_decision_summary():
    state = {'attempt_log': ['strike=1 summary=ok', 'strike=1 fast_check exit_code=0']}

    result = nodes.advance_node(state)

    assert len(result['decision_ledger']) == 1
    assert isinstance(result['decision_ledger'][0], DecisionSummary)


def test_attempt_log_does_not_survive_into_ledger_entry():
    state = {'attempt_log': ['line-1', 'line-2', 'line-3']}

    result = nodes.advance_node(state)

    assert result['attempt_log'] == []
    entry = result['decision_ledger'][0]
    assert entry.attempt_count == 3
    # The content is distilled into the summary string...
    assert 'line-1' in entry.summary
    # ...but the raw per-attempt list itself is gone, not just relocated.
    assert not hasattr(entry, 'attempt_log')
    assert isinstance(entry.summary, str)


def test_n_tickets_grow_ledger_by_n_entries_while_state_stays_bounded():
    """The claim as the README states it: state size grows with the number
    of decisions made, not with the tokens/lines spent making them. Run N
    tickets, each with its own sizeable attempt_log, through advance_node
    sequentially (as the graph would across a run) and check both halves of
    the claim: the ledger grows by exactly N, and the live attempt_log the
    graph is carrying never accumulates across tickets.
    """
    state: dict = {}
    ticket_count = 25
    lines_per_ticket = 40

    for i in range(ticket_count):
        state['attempt_log'] = [f'ticket-{i} line-{j}' for j in range(lines_per_ticket)]
        result = nodes.advance_node(state)
        state.update(result)
        # Bounded, not accumulating: right after every single ticket, the
        # live log is empty regardless of how many tickets came before.
        assert state['attempt_log'] == []

    ledger = state['decision_ledger']
    assert len(ledger) == ticket_count

    for i, entry in enumerate(ledger):
        assert entry.attempt_count == lines_per_ticket
        # Each entry is scoped to its own ticket's log...
        assert f'ticket-{i} line-0' in entry.summary
        # ...not a running concatenation of every ticket that preceded it.
        if i > 0:
            assert f'ticket-{i - 1} line-0' not in entry.summary
