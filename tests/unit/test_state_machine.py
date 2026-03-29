import pytest

from cogniq.domain.enums import IssueStatus
from cogniq.domain.state_machine import StateMachine


class TestStateMachine:
    """Test all valid and invalid state transitions."""

    # ── Valid transitions ──

    def test_backlog_to_in_analysis(self):
        assert StateMachine.can_transition(IssueStatus.BACKLOG, IssueStatus.IN_ANALYSIS)

    def test_in_analysis_to_plan_complete(self):
        assert StateMachine.can_transition(IssueStatus.IN_ANALYSIS, IssueStatus.PLAN_COMPLETE)

    def test_plan_complete_to_plan_approved(self):
        assert StateMachine.can_transition(IssueStatus.PLAN_COMPLETE, IssueStatus.PLAN_APPROVED)

    def test_plan_approved_to_in_progress(self):
        assert StateMachine.can_transition(IssueStatus.PLAN_APPROVED, IssueStatus.IN_PROGRESS)

    def test_in_progress_to_in_review(self):
        assert StateMachine.can_transition(IssueStatus.IN_PROGRESS, IssueStatus.IN_REVIEW)

    def test_in_review_to_done(self):
        assert StateMachine.can_transition(IssueStatus.IN_REVIEW, IssueStatus.DONE)

    def test_in_progress_to_blocked(self):
        assert StateMachine.can_transition(IssueStatus.IN_PROGRESS, IssueStatus.BLOCKED)

    def test_blocked_to_plan_approved(self):
        """Retry build from blocked state."""
        assert StateMachine.can_transition(IssueStatus.BLOCKED, IssueStatus.PLAN_APPROVED)

    def test_blocked_to_backlog(self):
        """Re-plan from blocked state."""
        assert StateMachine.can_transition(IssueStatus.BLOCKED, IssueStatus.BACKLOG)

    def test_plan_complete_to_backlog(self):
        """Gate 1 rejection."""
        assert StateMachine.can_transition(IssueStatus.PLAN_COMPLETE, IssueStatus.BACKLOG)

    def test_in_review_to_in_progress(self):
        """Gate 2 requested changes."""
        assert StateMachine.can_transition(IssueStatus.IN_REVIEW, IssueStatus.IN_PROGRESS)

    def test_cancelled_to_backlog(self):
        """Reopen cancelled issue."""
        assert StateMachine.can_transition(IssueStatus.CANCELLED, IssueStatus.BACKLOG)

    # ── Any status can go to cancelled ──

    @pytest.mark.parametrize("status", [
        IssueStatus.BACKLOG,
        IssueStatus.IN_ANALYSIS,
        IssueStatus.PLAN_COMPLETE,
        IssueStatus.PLAN_APPROVED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.IN_REVIEW,
        IssueStatus.BLOCKED,
    ])
    def test_any_to_cancelled(self, status):
        assert StateMachine.can_transition(status, IssueStatus.CANCELLED)

    # ── Invalid transitions ──

    def test_done_is_terminal(self):
        assert not StateMachine.can_transition(IssueStatus.DONE, IssueStatus.BACKLOG)
        assert not StateMachine.can_transition(IssueStatus.DONE, IssueStatus.IN_PROGRESS)

    def test_backlog_to_done_not_allowed(self):
        assert not StateMachine.can_transition(IssueStatus.BACKLOG, IssueStatus.DONE)

    def test_backlog_to_in_progress_not_allowed(self):
        """Must go through Plan first."""
        assert not StateMachine.can_transition(IssueStatus.BACKLOG, IssueStatus.IN_PROGRESS)

    def test_in_analysis_to_in_progress_not_allowed(self):
        """Must go through Gate 1."""
        assert not StateMachine.can_transition(IssueStatus.IN_ANALYSIS, IssueStatus.IN_PROGRESS)

    # ── validate_transition raises ──

    def test_validate_valid_transition(self):
        # Should not raise
        StateMachine.validate_transition(IssueStatus.BACKLOG, IssueStatus.IN_ANALYSIS)

    def test_validate_invalid_transition_raises(self):
        with pytest.raises(ValueError, match="Invalid transition"):
            StateMachine.validate_transition(IssueStatus.BACKLOG, IssueStatus.DONE)
