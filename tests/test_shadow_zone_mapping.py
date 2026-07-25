"""dashboard._shadow_direction_from_zone — legacy zone -> engine direction.

Regression coverage for a real bug caught during review: TREND_SCORE_MOVE_ZONE
is literally the string "SHORT_MOVE" (the NEUTRAL zone, score between -25 and
+25), and a substring check for "SHORT" would misclassify every neutral
reading as bearish. That would silently corrupt the agreement rate the whole
30-45 day shadow window exists to produce.
"""
from __future__ import annotations

import dashboard


def test_the_bullish_call_zone_maps_to_positive_direction():
    assert dashboard._shadow_direction_from_zone(
        dashboard.TREND_SCORE_CE_ZONE) == 1


def test_the_bearish_put_zone_maps_to_negative_direction():
    assert dashboard._shadow_direction_from_zone(
        dashboard.TREND_SCORE_PE_ZONE) == -1


def test_the_neutral_move_zone_maps_to_flat_not_bearish():
    """This is the exact bug: TREND_SCORE_MOVE_ZONE == "SHORT_MOVE" contains
    the substring "SHORT", but it is the NEUTRAL zone, not a bearish one."""
    assert dashboard.TREND_SCORE_MOVE_ZONE == "SHORT_MOVE"
    assert dashboard._shadow_direction_from_zone(
        dashboard.TREND_SCORE_MOVE_ZONE) == 0


def test_an_unrecognised_zone_is_flat_never_a_guess():
    assert dashboard._shadow_direction_from_zone("something_unexpected") == 0
    assert dashboard._shadow_direction_from_zone("") == 0
    assert dashboard._shadow_direction_from_zone(None) == 0
