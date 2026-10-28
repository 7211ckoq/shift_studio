"""ソフト条件の4段階の優先度と、旧形式（数値の重み）からの変換。"""

import pandas as pd
import pytest

from app import (PRIORITY_LEVELS, apply_weekly_preferences, parse_pairs, parse_same_shift_pairs,
                 priority_breakdown, priority_label, with_priority_column)
from optimizer import Employee


def test_levels_are_ordered_and_round_trip():
    assert list(PRIORITY_LEVELS) == ["低め", "普通", "高め", "最優先"]
    weights = list(PRIORITY_LEVELS.values())
    assert weights == sorted(weights) and len(set(weights)) == 4
    for level, weight in PRIORITY_LEVELS.items():
        assert priority_label(weight) == level


@pytest.mark.parametrize("weight, level", [
    (1, "低め"), (4, "低め"), (5, "普通"), (19, "普通"), (20, "高め"), (49, "高め"), (50, "最優先"), (100, "最優先"),
])
def test_legacy_weights_map_to_documented_ranges(weight, level):
    assert priority_label(weight) == level


def test_legacy_weight_columns_are_converted():
    legacy = pd.DataFrame([{"職員ID A": "S01", "職員ID B": "S02", "重み": "17"},
                           {"職員ID A": "S01", "職員ID B": "S03", "重み": ""}])
    converted = with_priority_column(legacy, "回避ペア")
    assert list(converted.columns) == ["職員ID A", "職員ID B", "優先度"]
    assert [pair.weight for pair in parse_pairs(legacy)] == [5, 5]
    same = pd.DataFrame([{"職員ID A": "S01", "職員ID B": "S02", "条件": "ソフト（できるだけ）",
                          "適用範囲": "", "重み": 60}])
    assert parse_same_shift_pairs(same)[0].weight == 100
    people = [Employee("S01", "A"), Employee("S02", "B")]
    preferences = pd.DataFrame([{"職員ID": "S01", "回避シフト": '["日勤"]', "重み": "25"}])
    updated = apply_weekly_preferences(people, pd.DataFrame(columns=["職員ID", "曜日", "シフト"]), preferences)
    assert updated[0].shift_avoidance_weight == 20


def test_new_priority_column_wins_and_blank_defaults_to_normal():
    frame = pd.DataFrame([{"職員ID A": "S01", "職員ID B": "S02", "優先度": "", "重み": 100}])
    assert parse_pairs(frame)[0].weight == PRIORITY_LEVELS["普通"]


def test_breakdown_lists_counts_from_highest_level():
    assert priority_breakdown([100, 20, 20, 1]) == "最優先 1 / 高め 2 / 普通 0 / 低め 1"
    assert priority_breakdown([]) == "最優先 0 / 高め 0 / 普通 0 / 低め 0"
