import pytest

from museroute.calculator import estimate_monthly_costs


def _inputs(**overrides):
    values = {
        "monthly_requests": 1000,
        "average_prompt_tokens": 100,
        "average_completion_tokens": 50,
        "cheap_input_price_per_million": 1,
        "cheap_output_price_per_million": 2,
        "strong_input_price_per_million": 10,
        "strong_output_price_per_million": 20,
        "escalation_rate": 0.2,
        "museroute_accuracy": 0.79,
        "strong_accuracy": 0.8,
    }
    values.update(overrides)
    return values


def test_formula_adds_strong_cost_only_for_escalated_requests():
    result = estimate_monthly_costs(**_inputs())

    # Cheap tier is used for all 1,000 requests: $0.20. Strong tier is used
    # for 20%: $0.40. Combined projected MuseRoute cost is $0.60.
    assert result["strong_only_monthly_cost"] == pytest.approx(2.0)
    assert result["museroute_monthly_cost"] == pytest.approx(0.6)
    assert result["projected_savings"] == pytest.approx(1.4)
    assert result["quality_gate_pass"] is True


def test_quality_drop_over_two_points_suppresses_savings():
    result = estimate_monthly_costs(**_inputs(museroute_accuracy=0.77, strong_accuracy=0.8))

    assert result["status"] == "quality_drop_too_large"
    assert result["museroute_monthly_cost"] == pytest.approx(0.6)
    assert result["projected_savings"] is None


def test_missing_price_keeps_projection_null():
    result = estimate_monthly_costs(**_inputs(cheap_input_price_per_million=None))

    assert result["status"] == "inputs_not_measured"
    assert result["museroute_monthly_cost"] is None
    assert result["projected_savings"] is None
