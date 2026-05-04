"""Strategy-specific config validation.

Thin module that re-exports the generic helpers from `cozempic._validation`
and adds `coerce_ordered_pair`, which is specific to strategy config dicts
holding related age thresholds (e.g. `tool_result_mid_age` and
`tool_result_old_age`).

The generic helpers (`coerce_non_negative_int`, `coerce_choice`, `ConfigError`)
were originally introduced here in PR #79 and later lifted to the root module
so argparse validators and env-var parsers can reuse the same error-message
shape. Existing strategy imports (`from ._config import coerce_non_negative_int`)
continue to work unchanged via the re-exports below.
"""

from __future__ import annotations

from .._validation import (
    ConfigError,
    coerce_choice,
    coerce_non_negative_int,
    coerce_positive_float,
    coerce_positive_int,
)

__all__ = [
    "ConfigError",
    "coerce_choice",
    "coerce_non_negative_int",
    "coerce_ordered_pair",
    "coerce_positive_float",
    "coerce_positive_int",
]


def coerce_ordered_pair(
    config: dict,
    low_key: str,
    high_key: str,
    defaults: tuple[int, int],
) -> tuple[int, int]:
    """Return `(config[low_key], config[high_key])` with the invariant
    `low < high` enforced. Missing keys fall back to `defaults`.

    Raises `ConfigError` if either value is the wrong type, negative, or
    if `low >= high`. Swapped pairs are the canonical source of silent
    misconfiguration (e.g. `tool_result_mid_age=50, tool_result_old_age=30`
    collapses the "recent" tier and marks every tool result as `old`).
    """
    low = coerce_non_negative_int(config, low_key, defaults[0])
    high = coerce_non_negative_int(config, high_key, defaults[1])
    if low >= high:
        raise ConfigError(
            f"config[{low_key!r}]={low} must be strictly less than "
            f"config[{high_key!r}]={high}"
        )
    return low, high
