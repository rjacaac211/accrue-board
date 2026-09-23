import itertools

import pytest

from accrueboard.domain.lifecycle import (
    TRANSITIONS,
    Actor,
    InvalidTransitionError,
    TaskState,
    allowed_targets,
    check_transition,
    is_terminal,
)

S = TaskState
M = Actor.MACHINE
H = Actor.HUMAN

EXPECTED: set[tuple[TaskState, TaskState, Actor]] = {
    (S.QUEUED, S.PROCESSING, M),
    (S.PROCESSING, S.AUTO_APPROVED, M),
    (S.PROCESSING, S.NEEDS_REVIEW, M),
    (S.PROCESSING, S.BLOCKED, M),
    (S.PROCESSING, S.FAILED, M),
    (S.PROCESSING, S.QUEUED, M),
    (S.AUTO_APPROVED, S.POSTED, M),
    (S.AUTO_APPROVED, S.NEEDS_REVIEW, M),
    (S.NEEDS_REVIEW, S.APPROVED, H),
    (S.NEEDS_REVIEW, S.REJECTED, H),
    (S.NEEDS_REVIEW, S.BLOCKED, H),
    (S.BLOCKED, S.NEEDS_REVIEW, H),
    (S.APPROVED, S.POSTED, M),
    (S.APPROVED, S.NEEDS_REVIEW, M),
    (S.FAILED, S.QUEUED, M),
    (S.FAILED, S.QUEUED, H),
    (S.POSTED, S.NEEDS_REVIEW, H),
}


def test_transition_table_is_exactly_as_designed() -> None:
    actual = {(src, dst, actor) for (src, dst), actors in TRANSITIONS.items() for actor in actors}
    assert actual == EXPECTED


@pytest.mark.parametrize(
    ("src", "dst", "actor"),
    [
        (src, dst, actor)
        for src, dst, actor in itertools.product(TaskState, TaskState, Actor)
        if (src, dst, actor) not in EXPECTED
    ],
)
def test_every_other_transition_is_rejected(src: TaskState, dst: TaskState, actor: Actor) -> None:
    with pytest.raises(InvalidTransitionError):
        check_transition(src, dst, actor)


@pytest.mark.parametrize(("src", "dst", "actor"), sorted(EXPECTED))
def test_every_designed_transition_is_allowed(src: TaskState, dst: TaskState, actor: Actor) -> None:
    check_transition(src, dst, actor)


def test_humans_cannot_auto_approve() -> None:
    with pytest.raises(InvalidTransitionError, match="human"):
        check_transition(S.PROCESSING, S.AUTO_APPROVED, H)


def test_machines_cannot_approve_reviewed_items() -> None:
    # Human decisions are final and only humans make them.
    with pytest.raises(InvalidTransitionError, match="machine"):
        check_transition(S.NEEDS_REVIEW, S.APPROVED, M)


def test_rejected_is_terminal() -> None:
    assert is_terminal(S.REJECTED)
    assert allowed_targets(S.REJECTED, H) == frozenset()
    assert not is_terminal(S.POSTED)  # can be reopened


def test_allowed_targets_for_reviewer() -> None:
    assert allowed_targets(S.NEEDS_REVIEW, H) == {S.APPROVED, S.REJECTED, S.BLOCKED}


def test_every_state_is_reachable_from_queued() -> None:
    reachable = {S.QUEUED}
    frontier = [S.QUEUED]
    while frontier:
        src = frontier.pop()
        for a, b in TRANSITIONS:
            if a == src and b not in reachable:
                reachable.add(b)
                frontier.append(b)
    assert reachable == set(TaskState)
