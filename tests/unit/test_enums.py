from cogniq_shared.domain.enums import IssueStatus


class TestIssueStatus:
    """Backend status is passed directly to frontend — no mapping needed."""

    def test_all_statuses_are_string_enum(self):
        for status in IssueStatus:
            assert isinstance(status.value, str)

    def test_status_values(self):
        assert IssueStatus.BACKLOG.value == "backlog"
        assert IssueStatus.IN_ANALYSIS.value == "in_analysis"
        assert IssueStatus.PLAN_COMPLETE.value == "plan_complete"
        assert IssueStatus.PLAN_APPROVED.value == "plan_approved"
        assert IssueStatus.IN_PROGRESS.value == "in_progress"
        assert IssueStatus.IN_REVIEW.value == "in_review"
        assert IssueStatus.BLOCKED.value == "blocked"
        assert IssueStatus.DONE.value == "done"
        assert IssueStatus.CANCELLED.value == "cancelled"

    def test_status_count(self):
        assert len(IssueStatus) == 9

    def test_status_from_string(self):
        assert IssueStatus("backlog") == IssueStatus.BACKLOG
        assert IssueStatus("in_analysis") == IssueStatus.IN_ANALYSIS
        assert IssueStatus("plan_complete") == IssueStatus.PLAN_COMPLETE

    def test_invalid_status_raises(self):
        import pytest
        with pytest.raises(ValueError):
            IssueStatus("invalid")
