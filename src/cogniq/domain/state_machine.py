from cogniq.domain.enums import IssueStatus

# Allowed state transitions: { from_status: [to_status, ...] }
ALLOWED_TRANSITIONS: dict[IssueStatus, list[IssueStatus]] = {
    IssueStatus.BACKLOG: [
        IssueStatus.IN_ANALYSIS,
        IssueStatus.CANCELLED,
    ],
    IssueStatus.IN_ANALYSIS: [
        IssueStatus.PLAN_COMPLETE,
        IssueStatus.BACKLOG,       # Plan failure
        IssueStatus.CANCELLED,
    ],
    IssueStatus.PLAN_COMPLETE: [
        IssueStatus.PLAN_APPROVED,  # Gate 1 approved
        IssueStatus.BACKLOG,        # Gate 1 rejected
        IssueStatus.CANCELLED,
    ],
    IssueStatus.PLAN_APPROVED: [
        IssueStatus.IN_PROGRESS,
        IssueStatus.BACKLOG,        # Re-plan requested
        IssueStatus.CANCELLED,
    ],
    IssueStatus.IN_PROGRESS: [
        IssueStatus.IN_REVIEW,      # Build completed, PR created
        IssueStatus.BLOCKED,        # Build failed
        IssueStatus.CANCELLED,
    ],
    IssueStatus.IN_REVIEW: [
        IssueStatus.DONE,           # Gate 2 merged
        IssueStatus.IN_PROGRESS,    # Gate 2 requested changes
        IssueStatus.CANCELLED,
    ],
    IssueStatus.BLOCKED: [
        IssueStatus.PLAN_APPROVED,  # Retry build
        IssueStatus.BACKLOG,        # Re-plan from scratch
        IssueStatus.CANCELLED,
    ],
    IssueStatus.DONE: [],
    IssueStatus.CANCELLED: [
        IssueStatus.BACKLOG,        # Reopen
    ],
}


class StateMachine:
    @staticmethod
    def can_transition(from_status: IssueStatus, to_status: IssueStatus) -> bool:
        allowed = ALLOWED_TRANSITIONS.get(from_status, [])
        return to_status in allowed

    @staticmethod
    def validate_transition(from_status: IssueStatus, to_status: IssueStatus) -> None:
        if not StateMachine.can_transition(from_status, to_status):
            allowed = ALLOWED_TRANSITIONS.get(from_status, [])
            allowed_str = ", ".join(s.value for s in allowed) if allowed else "none"
            raise ValueError(
                f"Invalid transition: {from_status.value} -> {to_status.value}. "
                f"Allowed: {allowed_str}"
            )
