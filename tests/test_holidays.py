"""休日（土日祝の初期値・カレンダー切替・リセット）と作成期間の範囲選択のテスト。"""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from jp_holidays import default_closed_days, holiday_name, holidays_in_year
from optimizer import (
    Assignment, Employee, Settings, WeeklyAssignment, audit_schedule, solve_schedule, validate_inputs,
)


@pytest.mark.parametrize("day, name", [
    (date(2026, 1, 1), "元日"), (date(2026, 1, 12), "成人の日"), (date(2026, 2, 23), "天皇誕生日"),
    (date(2026, 3, 20), "春分の日"), (date(2026, 5, 6), "振替休日"), (date(2026, 7, 20), "海の日"),
    (date(2026, 9, 22), "国民の休日"), (date(2026, 9, 23), "秋分の日"), (date(2026, 10, 12), "スポーツの日"),
    (date(2025, 2, 24), "振替休日"), (date(2025, 11, 24), "振替休日"),
    (date(2019, 4, 30), "国民の休日"), (date(2019, 5, 1), "天皇の即位の日"),
    (date(2020, 7, 24), "スポーツの日"), (date(2021, 8, 9), "振替休日"),
])
def test_japanese_holidays(day, name):
    assert holiday_name(day) == name


def test_holiday_counts_and_non_holidays():
    assert len(holidays_in_year(2026)) == 18
    assert holiday_name(date(2026, 11, 2)) is None
    assert holiday_name(date(2026, 12, 23)) is None


def test_default_closed_days_are_weekends_and_holidays():
    dates = [date(2026, 11, 1) + timedelta(days=i) for i in range(7)]
    # 11/1(日) 11/3(火・文化の日) 11/7(土)
    assert default_closed_days(dates) == {date(2026, 11, 1), date(2026, 11, 3), date(2026, 11, 7)}


def team(size=8):
    return [Employee(f"S{i:02}", f"職員{i}", True) for i in range(size)]


def config(**changes):
    return replace(Settings(date(2026, 11, 2), days=7, time_limit_seconds=5), **changes)


def test_no_shifts_on_closed_days():
    closed = frozenset({date(2026, 11, 3), date(2026, 11, 7), date(2026, 11, 8)})
    settings = config(closed_days=closed)
    employees = team()
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert not any(a.day in closed for a in result.assignments)
    assert len(result.assignments) == 4 * 3 * settings.minimum_staff
    bad = (*result.assignments, Assignment("S00", date(2026, 11, 3), "日勤"))
    assert any("休日に勤務" in error for error in audit_schedule(employees, settings, bad))


def test_closed_days_skip_weekly_assignment_and_relax_consecutive_limit():
    closed = frozenset({date(2026, 11, 3)})
    employees = team()
    employees[0] = replace(employees[0], weekly_assignments=(WeeklyAssignment(1, "日勤"),))  # 火曜固定
    result = solve_schedule(employees, config(closed_days=closed))
    assert result.has_solution, result.messages


def test_closed_days_type_validation():
    assert any("休日" in error for error in validate_inputs(team(), config(closed_days=("2026-11-03",))))


def holiday_app():
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45).run()
    assert not at.exception
    at.date_input[0].set_value((date(2026, 11, 1), date(2026, 11, 7))).run()
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(5).run()
    assert not at.exception
    return at


def holiday_button(at, day):
    return at.button(key=f"holiday_{day.isoformat()}")


def test_ui_defaults_to_weekends_and_holidays_and_toggles_and_resets():
    at = holiday_app()
    assert at.session_state.holiday_base is True
    sunday, holiday, monday = date(2026, 11, 1), date(2026, 11, 3), date(2026, 11, 2)
    assert holiday_button(at, sunday).label.startswith("1 休")
    assert holiday_button(at, holiday).label.startswith("3 休")
    assert holiday_button(at, monday).label.startswith("2 出")
    # 休日→出勤日、出勤日→休日の切替。
    holiday_button(at, holiday).click().run()
    holiday_button(at, monday).click().run()
    assert not at.exception
    assert holiday_button(at, holiday).label == "3 出 ✎"
    assert holiday_button(at, monday).label == "2 休 ✎"
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    result = at.session_state.schedule[1]
    assert result.has_solution, result.messages
    worked = {a.day for a in result.assignments}
    assert worked == {holiday, date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)}
    roster = next(f.value for f in at.dataframe if "役職" in f.value.columns)
    assert (roster["11/01(日)"] == "休日").all()
    # リセットで土日祝に戻る。
    next(b for b in at.button if b.label == "土日祝の休みにリセット").click().run()
    assert at.session_state.holiday_toggles == set()
    assert holiday_button(at, holiday).label == "3 休"
    assert holiday_button(at, monday).label == "2 出"
    assert any("入力条件が変更" in w.value for w in at.warning)
    next(b for b in at.button if b.label == "すべて出勤日にする").click().run()
    assert holiday_button(at, sunday).label == "1 出"


def test_ui_period_range_and_incomplete_selection():
    at = holiday_app()
    at.date_input[0].set_value((date(2026, 11, 1),)).run()
    assert not at.exception
    assert any("終了日" in e.value for e in at.error)
    assert next(b for b in at.button if b.label == "シフトを作成").disabled
    at.date_input[0].set_value((date(2026, 11, 1), date(2027, 1, 15))).run()
    assert any("62日以内" in e.value for e in at.error)
    assert next(b for b in at.button if b.label == "シフトを作成").disabled
    at.date_input[0].set_value((date(2026, 11, 1), date(2026, 12, 31))).run()
    assert not at.error
    assert not next(b for b in at.button if b.label == "シフトを作成").disabled
