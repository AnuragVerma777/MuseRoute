"""Projection math for the UI's editable monthly savings calculator."""

from __future__ import annotations

import math
from typing import Any


def _valid_nonnegative(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def estimate_monthly_costs(
    *,
    monthly_requests: Any,
    average_prompt_tokens: Any,
    average_completion_tokens: Any,
    cheap_input_price_per_million: Any,
    cheap_output_price_per_million: Any,
    strong_input_price_per_million: Any,
    strong_output_price_per_million: Any,
    escalation_rate: Any,
    museroute_accuracy: Any,
    strong_accuracy: Any,
) -> dict[str, Any]:
    """Return projected costs, suppressing savings when quality misses the guard.

    The guard is one-sided: routing must be no more than two percentage points
    below strong-only accuracy. All price and workload inputs are required for
    dollar projections; missing values remain ``None``.
    """
    input_names = (
        "monthly_requests",
        "average_prompt_tokens",
        "average_completion_tokens",
        "cheap_input_price_per_million",
        "cheap_output_price_per_million",
        "strong_input_price_per_million",
        "strong_output_price_per_million",
    )
    inputs = {
        "monthly_requests": monthly_requests,
        "average_prompt_tokens": average_prompt_tokens,
        "average_completion_tokens": average_completion_tokens,
        "cheap_input_price_per_million": cheap_input_price_per_million,
        "cheap_output_price_per_million": cheap_output_price_per_million,
        "strong_input_price_per_million": strong_input_price_per_million,
        "strong_output_price_per_million": strong_output_price_per_million,
    }
    valid = all(_valid_nonnegative(inputs[name]) for name in input_names)
    valid = valid and isinstance(monthly_requests, int) and not isinstance(monthly_requests, bool)
    valid = valid and _valid_nonnegative(escalation_rate) and escalation_rate <= 1
    accuracies_measured = _valid_nonnegative(museroute_accuracy) and _valid_nonnegative(strong_accuracy)
    quality_gap = round((strong_accuracy - museroute_accuracy) * 100, 4) if accuracies_measured else None
    quality_pass = museroute_accuracy >= strong_accuracy - 0.02 if accuracies_measured else None

    strong_only_cost = None
    museroute_cost = None
    savings = None
    if valid:
        request_count = monthly_requests
        prompt_tokens = average_prompt_tokens
        completion_tokens = average_completion_tokens
        cheap_per_request = (
            prompt_tokens * cheap_input_price_per_million
            + completion_tokens * cheap_output_price_per_million
        ) / 1_000_000
        strong_per_request = (
            prompt_tokens * strong_input_price_per_million
            + completion_tokens * strong_output_price_per_million
        ) / 1_000_000
        strong_only_cost = request_count * strong_per_request
        museroute_cost = request_count * (cheap_per_request + escalation_rate * strong_per_request)
        if quality_pass:
            savings = strong_only_cost - museroute_cost

    if quality_pass is False:
        status = "quality_drop_too_large"
    elif quality_pass is None:
        status = "quality_not_measured"
    elif not valid:
        status = "inputs_not_measured"
    else:
        status = "projection_available"

    return {
        "status": status,
        "quality_gate_pass": quality_pass,
        "quality_gap_percentage_points": quality_gap,
        "strong_only_monthly_cost": strong_only_cost,
        "museroute_monthly_cost": museroute_cost,
        "projected_savings": savings,
    }
