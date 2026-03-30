import pytest

from cogniq_worker.agents.base import CostLimitExceeded, CostTracker


class TestCostTracker:
    def test_add_tracks_tokens(self):
        tracker = CostTracker(max_usd=10.0)
        tracker.add(1000, 500, "claude-sonnet-4-20250514")
        assert tracker.total_input_tokens == 1000
        assert tracker.total_output_tokens == 500
        assert tracker.total_tokens == 1500
        assert tracker.total_usd > 0

    def test_add_calculates_cost(self):
        tracker = CostTracker(max_usd=10.0)
        # Sonnet pricing: $3/M input, $15/M output
        cost = tracker.add(1_000_000, 0, "claude-sonnet-4-20250514")
        assert abs(cost - 3.0) < 0.01

    def test_cost_limit_exceeded(self):
        tracker = CostTracker(max_usd=0.01)
        with pytest.raises(CostLimitExceeded):
            tracker.add(1_000_000, 1_000_000, "claude-sonnet-4-20250514")

    def test_under_limit_no_error(self):
        tracker = CostTracker(max_usd=100.0)
        tracker.add(1000, 1000, "claude-sonnet-4-20250514")
        # Should not raise

    def test_default_pricing_for_unknown_model(self):
        tracker = CostTracker(max_usd=10.0)
        cost = tracker.add(1000, 1000, "unknown-model")
        assert cost > 0

    def test_cumulative_cost(self):
        tracker = CostTracker(max_usd=10.0)
        tracker.add(100, 100, "claude-sonnet-4-20250514")
        first = tracker.total_usd
        tracker.add(100, 100, "claude-sonnet-4-20250514")
        assert tracker.total_usd > first
