"""Task lifecycle state machine.

    Queued -> Processing -> AutoApproved -> Posted
                         -> NeedsReview -> Approved -> Posted
                                        -> Rejected            (terminal)
                                        <-> Blocked
                         -> Blocked                             (pipeline: missing information)
                         -> Failed -> Queued                    (retry)
                         -> Queued                              (worker lease expired)
    Posted -> NeedsReview                                       (reopen, with a reversing entry)
    AutoApproved / Approved -> NeedsReview                      (posting refused; needs a human)

Each transition also names who may make it. Machines never approve or reject reviewed work,
and humans never auto-approve: a human decision is final and is not re-scored.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class TaskState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    FAILED = "failed"
    POSTED = "posted"


class Actor(StrEnum):
    MACHINE = "machine"
    HUMAN = "human"


class InvalidTransitionError(ValueError):
    pass


_S = TaskState
_M = frozenset({Actor.MACHINE})
_H = frozenset({Actor.HUMAN})
_ANY = frozenset(Actor)

TRANSITIONS: Mapping[tuple[TaskState, TaskState], frozenset[Actor]] = MappingProxyType(
    {
        (_S.QUEUED, _S.PROCESSING): _M,
        (_S.PROCESSING, _S.AUTO_APPROVED): _M,
        (_S.PROCESSING, _S.NEEDS_REVIEW): _M,
        (_S.PROCESSING, _S.BLOCKED): _M,
        (_S.PROCESSING, _S.FAILED): _M,
        (_S.PROCESSING, _S.QUEUED): _M,
        (_S.AUTO_APPROVED, _S.POSTED): _M,
        (_S.AUTO_APPROVED, _S.NEEDS_REVIEW): _M,
        (_S.NEEDS_REVIEW, _S.APPROVED): _H,
        (_S.NEEDS_REVIEW, _S.REJECTED): _H,
        (_S.NEEDS_REVIEW, _S.BLOCKED): _H,
        (_S.BLOCKED, _S.NEEDS_REVIEW): _H,
        (_S.APPROVED, _S.POSTED): _M,
        (_S.APPROVED, _S.NEEDS_REVIEW): _M,
        (_S.FAILED, _S.QUEUED): _ANY,
        (_S.POSTED, _S.NEEDS_REVIEW): _H,
    }
)


def check_transition(current: TaskState, target: TaskState, actor: Actor) -> None:
    """Raise InvalidTransitionError unless ``actor`` may move ``current`` to ``target``."""
    actors = TRANSITIONS.get((current, target))
    if actors is None:
        raise InvalidTransitionError(f"no transition from {current.value} to {target.value}")
    if actor not in actors:
        raise InvalidTransitionError(
            f"a {actor.value} actor may not move a task from {current.value} to {target.value}"
        )


def allowed_targets(current: TaskState, actor: Actor) -> frozenset[TaskState]:
    return frozenset(
        dst for (src, dst), actors in TRANSITIONS.items() if src == current and actor in actors
    )


def is_terminal(state: TaskState) -> bool:
    return not any(src == state for src, _ in TRANSITIONS)
