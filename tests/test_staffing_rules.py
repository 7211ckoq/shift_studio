"""日付・曜日・シフトごとの必要人数（最低・最大）のテスト。"""

from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays
from optimizer import (
    Assignment, Employee, RoleRequirement, Settings, StaffingRule, audit_schedule, solve_schedule,
    staff_bounds, validate_inputs,
)


MONDAY = date(2026, 11, 2)


def team(size=12, leaders=12):
    return [Employee(f"S{i:02}", f"職員{i}", i < leaders) for i in range(size)]


def config(**changes):
    base = Settings(MONDAY, days=7, minimum_staff=1, minimum_rest_hours=0, time_limit_seconds=10)
    return replace(base, **changes)


def counts(result):
    return Counter((a.day, a.shift) for a in result.assignments)


def test_priority_of_rules():
    rules = (
        StaffingRule(5),                                   # 毎日・全シフト
        StaffingRule(4, shift="日勤"),                      # 毎日・日勤
        StaffingRule(3, 3, weekday=5),                     # 土曜・全シフト
        StaffingRule(2, 2, weekday=5, shift="日勤"),        # 土曜・日勤
        StaffingRule(1, 1, day=MONDAY + timedelta(days=5)),  # 11/7(土)・全シフト
        StaffingRule(0, 0, day=MONDAY + timedelta(days=5), shift="深夜"),
    )
    settings = config(staffing_rules=rules, maximum_staff=9)
    assert staff_bounds(config(maximum_staff=9), MONDAY, "日勤") == (1, 9)
    assert staff_bounds(settings, MONDAY, "深夜") == (5, None)
    assert staff_bounds(settings, MONDAY, "日勤") == (4, None)
    saturday = MONDAY + timedelta(days=5)
    assert staff_bounds(settings, saturday, "深夜") == (0, 0)
    assert staff_bounds(settings, saturday, "日勤") == (1, 1)
    assert staff_bounds(settings, saturday + timedelta(days=7), "日勤") == (2, 2)  # 範囲外でも曜日規則
    assert staff_bounds(settings, saturday + timedelta(days=7), "準夜") == (3, 3)


def test_exact_and_maximum_counts_are_hard():
    rules = (StaffingRule(3, 3, day=MONDAY), StaffingRule(1, 1, shift="準夜"),
             StaffingRule(0, 0, day=MONDAY + timedelta(days=1), shift="深夜"))
    settings = config(days=3, staffing_rules=rules, maximum_staff=2)
    employees = team()
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert audit_schedule(employees, settings, result.assignments) == []
    c = counts(result)
    assert c[MONDAY, "深夜"] == c[MONDAY, "日勤"] == c[MONDAY, "準夜"] == 3
    assert c[MONDAY + timedelta(days=1), "深夜"] == 0
    assert c[MONDAY + timedelta(days=2), "準夜"] == 1
    too_many = (*result.assignments, *[Assignment(f"S{i:02}", MONDAY + timedelta(days=2), "準夜")
                                       for i in range(12) if (f"S{i:02}", MONDAY + timedelta(days=2))
                                       not in {(a.employee_id, a.day) for a in result.assignments}][:1])
    assert any("最大人数" in error for error in audit_schedule(employees, settings, too_many))


def test_maximum_below_required_role_is_infeasible():
    settings = config(days=1, role_requirements=(RoleRequirement("主任", "each_shift", 2),),
                      staffing_rules=(StaffingRule(1, 1),))
    employees = [replace(person, roles=frozenset({"主任"})) for person in team()]
    assert solve_schedule(employees, settings).status == "infeasible"


def test_zero_minimum_shift_needs_role_only_when_staffed():
    settings = config(days=1, role_requirements=(RoleRequirement("主任", "each_shift", 1),),
                      staffing_rules=(StaffingRule(0, shift="深夜"),))
    employees = [replace(person, roles=frozenset({"主任"} if i == 0 else set())) for i, person in enumerate(team(4))]
    # 主任は1人だけなので、日勤・準夜の両方を満たせず作成不可。
    assert solve_schedule(employees, settings).status == "infeasible"
    settings = replace(settings, staffing_rules=(StaffingRule(0, shift="深夜"), StaffingRule(0, shift="準夜")))
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert {a.shift for a in result.assignments} == {"日勤"}
    # 0人可のシフトに主任なしで入れる配置は検査で拒否する。
    bad = (*result.assignments, Assignment("S03", MONDAY, "深夜"))
    assert any("主任" in error for error in audit_schedule(employees, settings, bad))


@pytest.mark.parametrize("rule, message", [
    (StaffingRule(2, 1), "最大人数は最低人数以上"),
    (StaffingRule(-1), "0〜100"),
    (StaffingRule(1, day=MONDAY, weekday=0), "片方だけ"),
    (StaffingRule(1, day=MONDAY + timedelta(days=30)), "作成期間内"),
    (StaffingRule(1, weekday=7), "曜日"),
    (StaffingRule(1, shift="早番"), "シフト"),
])
def test_invalid_rules(rule, message):
    errors = validate_inputs(team(), config(staffing_rules=(rule,)))
    assert any(message in error for error in errors), errors


def test_duplicate_rules_and_global_maximum_validation():
    errors = validate_inputs(team(), config(staffing_rules=(StaffingRule(1, weekday=5), StaffingRule(2, weekday=5))))
    assert any("重複" in error for error in errors)
    assert any("最大人数" in e for e in validate_inputs(team(), config(minimum_staff=3, maximum_staff=2)))


def ui_app():
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()
    at.date_input[0].set_value((MONDAY, MONDAY + timedelta(days=6))).run()
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(10).run()
    assert not at.exception
    return at


def test_ui_rules_preview_and_generation():
    at = ui_app()
    next(n for n in at.number_input if n.label == "各シフトの最大人数（0＝上限なし）").set_value(3).run()
    at.session_state.staffing_source = pd.DataFrame([
        {"日付": "", "曜日": "土", "シフト": "全シフト", "最低人数": 1, "最大人数": 1},
        {"日付": MONDAY.isoformat(), "曜日": "", "シフト": "日勤", "最低人数": 4, "最大人数": 4},
        {"日付": "", "曜日": "", "シフト": "深夜", "最低人数": 2, "最大人数": None},
    ])
    at.session_state.staffing_revision += 1
    at.run()
    assert not at.exception
    preview = next(f.value for f in at.dataframe if {"日付", "曜日", "日勤", "深夜"} <= set(f.value.columns))
    assert preview.set_index("日付").loc[MONDAY.isoformat(), "日勤"] == "4人"
    assert preview.set_index("日付").loc[MONDAY.isoformat(), "深夜"] == "2人以上"
    assert preview.set_index("日付").loc[MONDAY.isoformat(), "準夜"] == "2〜3人"
    assert preview.set_index("日付").loc["2026-11-07", "深夜"] == "1人"
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    result = at.session_state.schedule[1]
    assert result.has_solution, result.messages
    c = counts(result)
    assert c[MONDAY, "日勤"] == 4
    assert all(c[date(2026, 11, 7), shift] == 1 for shift in ("深夜", "日勤", "準夜"))
    assert all(value <= 4 for value in c.values())
    daily = next(f.value for f in at.dataframe if {"日付", "シフト", "人数", "必要人数"} <= set(f.value.columns))
    assert daily.loc[(daily["日付"] == MONDAY.isoformat()) & (daily["シフト"] == "日勤"), "必要人数"].iloc[0] == "4人"


def test_ui_invalid_rule_blocks_generation():
    at = ui_app()
    at.session_state.staffing_source = pd.DataFrame([
        {"日付": MONDAY.isoformat(), "曜日": "月", "シフト": "全シフト", "最低人数": 1, "最大人数": None},
    ])
    at.session_state.staffing_revision += 1
    at.run()
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    assert any("片方だけ" in e.value for e in [*at.error, *at.markdown])
    assert not at.success
