"""出勤開始時刻の非減少ルールをモデル・独立検査・画面で検証する。"""

import json
from dataclasses import fields, replace
from datetime import date, timedelta
from inspect import Parameter, signature
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays

from optimizer import (
    Assignment, Employee, Settings, ShiftTime, WeeklyAssignment, WorkTimeOverride,
    audit_schedule, employee_shift_time, solve_schedule, validate_inputs,
)


MONDAY = date(2026, 1, 26)
LABEL = "前日より早い出勤を禁止する"
RESULT_DOWNLOADS = {
    "シフト表 CSV", "日別配置 CSV", "職員別集計 CSV", "月別労働時間 CSV",
    "個別勤務明細 CSV", "ペア配慮結果 CSV", "曜日固定結果 CSV",
    "シフト回避結果 CSV", "作成条件を保存（JSON）",
}


def config(**changes):
    return replace(Settings(
        MONDAY, days=2, shift_names=("早", "遅"),
        shift_times=(ShiftTime(540, 600), ShiftTime(900, 960)),
        time_limit_seconds=5,
    ), **changes)


def scenario(settings, entries, overrides=(), **person_changes):
    """各区分の専任2人で人数を確保し、対象職員だけに比較を強制する。"""
    target = Employee(
        "target", "対象職員", True,
        weekly_assignments=tuple(WeeklyAssignment(day.weekday(), shift)
                                 for day, shift in entries),
        work_time_overrides=overrides,
    )
    staff = [replace(target, **person_changes)]
    for shift in settings.shift_names:
        staff.extend(Employee(f"{shift}-{i}", f"補助{shift}{i}", True,
                              allowed_shifts=frozenset({shift})) for i in range(2))
    return staff


def manual_assignments(staff, settings, entries):
    # ソルバーを呼ばず、十分な人数・役職を持つ独立検査用の割当を構築する。
    return tuple(
        Assignment(person.employee_id, day, shift)
        for person in staff[1:] for day in settings.dates
        for shift in settings.shift_names if shift in person.allowed_shifts
    ) + tuple(Assignment("target", day, shift) for day, shift in entries)


def assert_solved(staff, settings, entries):
    assert validate_inputs(staff, settings) == []
    result = solve_schedule(staff, settings)
    assert result.status == "optimal", result.messages
    assert audit_schedule(staff, settings, result.assignments) == []
    assert {Assignment("target", day, shift) for day, shift in entries} <= set(result.assignments)
    return result


def assert_forbidden(staff, settings, entries):
    disabled = replace(settings, nondecreasing_start_times=False)
    assert_solved(staff, disabled, entries)  # 休息・人数などの別要因ではない。
    assert validate_inputs(staff, settings) == []
    result = solve_schedule(staff, settings)
    assert result.status == "infeasible", result.messages
    given = manual_assignments(staff, settings, entries)
    assert audit_schedule(staff, disabled, given) == []
    errors = audit_schedule(staff, settings, given)
    assert len(errors) == 1, errors
    assert "出勤時刻が前日より早く" in errors[0]
    assert "対象職員" in errors[0] and str(entries[1][0]) in errors[0]


def test_flag_default_false_keyword_only_and_positional_compatibility():
    original = Settings(MONDAY)
    parameter = signature(Settings).parameters["nondecreasing_start_times"]
    assert parameter.kind is Parameter.KEYWORD_ONLY
    assert parameter.default is False
    assert original.nondecreasing_start_times is False
    assert next(f for f in fields(Settings) if f.name == "nondecreasing_start_times").kw_only
    positional = Settings(MONDAY, 2, 2, "each_shift", 5, 11, 5,
                          original.shift_times, original.shift_names, ())
    assert positional.avoid_pairs == ()
    assert positional.nondecreasing_start_times is False


def test_default_off_allows_forced_late_then_early_with_seventeen_hours_rest():
    settings = config()
    entries = [(MONDAY, "遅"), (MONDAY + timedelta(days=1), "早")]
    assert 1440 + 540 - 960 == 17 * 60
    staff = scenario(settings, entries)
    assert_solved(staff, settings, entries)
    assert audit_schedule(staff, settings, manual_assignments(staff, settings, entries)) == []


def test_enabled_rule_is_hard_even_for_weekly_forced_assignments():
    settings = config(nondecreasing_start_times=True)
    entries = [(MONDAY, "遅"), (MONDAY + timedelta(days=1), "早")]
    assert_forbidden(scenario(settings, entries), settings, entries)


@pytest.mark.parametrize("next_start", [540, 541, 600])
def test_same_or_later_effective_start_is_allowed_to_the_minute(next_start):
    settings = config(nondecreasing_start_times=True, shift_names=("勤務",),
                      shift_times=(ShiftTime(540, 600),))
    entries = [(day, "勤務") for day in settings.dates]
    overrides = (WorkTimeOverride("勤務", ShiftTime(next_start, next_start + 60), entries[1][0]),)
    staff = scenario(settings, entries, overrides)
    assert_solved(staff, settings, entries)
    assert audit_schedule(staff, settings, manual_assignments(staff, settings, entries)) == []


@pytest.mark.parametrize("dated_first", [False, True])
def test_same_category_date_override_overrides_recurring_time_regardless_of_row_order(dated_first):
    settings = config(nondecreasing_start_times=True, shift_names=("勤務",),
                      shift_times=(ShiftTime(480, 540),))
    entries = [(day, "勤務") for day in settings.dates]
    overrides = (WorkTimeOverride("勤務", ShiftTime(600, 660)),
                 WorkTimeOverride("勤務", ShiftTime(599, 659), entries[1][0]))
    if dated_first:
        overrides = overrides[::-1]
    staff = scenario(settings, entries, overrides)
    assert [employee_shift_time(staff[0], settings, day, shift).start_minute
            for day, shift in entries] == [600, 599]
    assert_forbidden(staff, settings, entries)


@pytest.mark.parametrize("first,second,allowed", [("遅", "早", False), ("早", "遅", True)])
def test_reversed_shift_tuple_compares_actual_minutes_not_shift_indices(first, second, allowed):
    settings = config(nondecreasing_start_times=True, shift_names=("遅", "早"),
                      shift_times=(ShiftTime(900, 960), ShiftTime(540, 600)))
    entries = [(MONDAY, first), (MONDAY + timedelta(days=1), second)]
    staff = scenario(settings, entries)
    if allowed:
        assert_solved(staff, settings, entries)
        assert audit_schedule(staff, settings, manual_assignments(staff, settings, entries)) == []
    else:
        assert_forbidden(staff, settings, entries)


def test_overnight_20_to_next_09_is_forbidden_despite_positive_physical_rest_and_rest_zero():
    settings = config(nondecreasing_start_times=True, minimum_rest_hours=0,
                      shift_names=("日勤", "夜勤"),
                      shift_times=(ShiftTime(540, 600), ShiftTime(1200, 1920)))
    entries = [(MONDAY, "夜勤"), (MONDAY + timedelta(days=1), "日勤")]
    assert 1440 + 540 - 1920 == 60  # 翌08時終了→09時開始は実時間でも重複しない。
    assert_forbidden(scenario(settings, entries), settings, entries)


@pytest.mark.parametrize("enabled", [False, True])
def test_equal_starts_do_not_bypass_the_separate_minimum_rest(enabled):
    settings = config(nondecreasing_start_times=enabled, minimum_rest_hours=24,
                      shift_names=("勤務",), shift_times=(ShiftTime(540, 600),))
    entries = [(day, "勤務") for day in settings.dates]
    staff = scenario(settings, entries)
    # 補助職員は日替わりにし、24時間休息の違反を対象職員だけに限定する。
    staff[1:] = [replace(p, days_off=frozenset({settings.dates[1]})) for p in staff[1:]]
    staff.extend(Employee(f"翌日-{i}", f"翌日補助{i}", True,
                          allowed_shifts=frozenset({"勤務"}),
                          days_off=frozenset({MONDAY})) for i in range(2))
    assert_solved(staff, replace(settings, minimum_rest_hours=23), entries)
    assert solve_schedule(staff, settings).status == "infeasible"
    people = {p.employee_id: p for p in staff}
    given = tuple(a for a in manual_assignments(staff, settings, entries)
                  if a.day not in people[a.employee_id].days_off)
    assert audit_schedule(staff, replace(settings, minimum_rest_hours=23), given) == []
    errors = audit_schedule(staff, settings, given)
    assert len(errors) == 1 and "休息" in errors[0]
    assert "対象職員" in errors[0]


@pytest.mark.parametrize("explicit_day_off", [False, True])
def test_day_without_assignment_resets_comparison_even_if_not_requested_off(explicit_day_off):
    settings = config(days=3, nondecreasing_start_times=True,
                      shift_names=("勤務",), shift_times=(ShiftTime(540, 600),))
    entries = [(MONDAY, "勤務"), (MONDAY + timedelta(days=2), "勤務")]
    staff = scenario(settings, entries,
                     (WorkTimeOverride("勤務", ShiftTime(1200, 1260), MONDAY),),
                     max_monthly_minutes=120,
                     days_off=frozenset({MONDAY + timedelta(days=1)}) if explicit_day_off else frozenset())
    result = assert_solved(staff, settings, entries)
    assert {a.day for a in result.assignments if a.employee_id == "target"} == {d for d, _ in entries}
    assert audit_schedule(staff, settings, manual_assignments(staff, settings, entries)) == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("rest_hours,allowed", [(10, True), (11, False)])
def test_off_start_date_does_not_discard_rest_check_for_gap_two(enabled, rest_hours, allowed):
    settings = config(days=3, nondecreasing_start_times=enabled,
                      minimum_rest_hours=rest_hours, shift_names=("勤務",),
                      shift_times=(ShiftTime(480, 960),))
    entries = [(MONDAY, "勤務"), (MONDAY + timedelta(days=2), "勤務")]
    # 月23時〜火22時→水08時。火曜開始の割当なしでも、休息は10時間だけ。
    staff = scenario(settings, entries,
                     (WorkTimeOverride("勤務", ShiftTime(1380, 2760), MONDAY),),
                     max_monthly_minutes=1860)
    assert 2 * 1440 + 480 - 2760 == 10 * 60
    given = manual_assignments(staff, settings, entries)
    if allowed:
        result = assert_solved(staff, settings, entries)
        assert len([a for a in result.assignments if a.employee_id == "target"]) == 2
        assert audit_schedule(staff, settings, given) == []
    else:
        assert validate_inputs(staff, settings) == []
        assert solve_schedule(staff, settings).status == "infeasible"
        errors = audit_schedule(staff, settings, given)
        assert len(errors) == 1 and "休息" in errors[0]
        assert "出勤時刻" not in errors[0]


@pytest.mark.parametrize("value", [1, None])
def test_flag_rejects_non_boolean_values_in_validation_solver_and_audit(value):
    settings = config(nondecreasing_start_times=value)
    staff = scenario(settings, [])
    errors = validate_inputs(staff, settings)
    assert len(errors) == 1 and "真偽値" in errors[0]
    result = solve_schedule(staff, settings)
    assert result.status == "invalid" and not result.assignments
    assert list(result.messages) == errors
    assert audit_schedule(staff, settings, ()) == errors


def test_month_boundary_does_not_reset_adjacent_start_date_comparison():
    settings = config(start_date=date(2026, 1, 31), nondecreasing_start_times=True)
    entries = [(settings.dates[0], "遅"), (settings.dates[1], "早")]
    assert [day.month for day, _ in entries] == [1, 2]
    assert_forbidden(scenario(settings, entries), settings, entries)


@pytest.mark.parametrize("start", [MONDAY, date(2026, 2, 1)])
def test_first_period_day_has_no_invented_previous_start_comparison(start):
    settings = config(start_date=start, days=1, nondecreasing_start_times=True,
                      shift_names=("早",), shift_times=(ShiftTime(1, 61),))
    entries = [(start, "早")]
    staff = scenario(settings, entries)
    assert_solved(staff, settings, entries)
    assert audit_schedule(staff, settings, manual_assignments(staff, settings, entries)) == []


def small_app(size=3, pattern="日勤のみ"):
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=30)).run()
    assert not at.exception
    at.date_input[0].set_value((MONDAY, MONDAY + timedelta(days=2 - 1)))
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(5)
    at.selectbox(key="shift_pattern").select(pattern)
    at.session_state.staff_source = at.session_state.staff_source.iloc[:size].copy()
    at.session_state.editor_revision += 1
    at.run()
    assert not at.exception
    return at


def generate(at):
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    return at


def downloads(at):
    return {element.proto.label for element in at.get("download_button")}


def app_solution(at):
    assert not at.exception and at.success
    fingerprint, result, staff, settings = at.session_state.schedule
    assert result.status == "optimal", result.messages
    assert settings.days == 2 and settings.time_limit_seconds == 5
    assert audit_schedule(staff, settings, result.assignments) == []
    assert RESULT_DOWNLOADS <= downloads(at)
    return fingerprint, result, staff, settings


def no_app_results(at):
    assert not at.exception and not at.success
    assert not (RESULT_DOWNLOADS & downloads(at))
    assert not any("実労働時間" in frame.value.columns for frame in at.dataframe)


@pytest.mark.parametrize("enabled", [False, True])
def test_app_checkbox_default_generation_and_saved_json_flag(enabled):
    at = small_app()
    checkbox = at.checkbox(key="nondecreasing_start_times")
    assert checkbox.label == LABEL and checkbox.value is False
    rules = next(exp for exp in at.expander if exp.label == "追加の勤務ルール")
    assert any(box.key == "nondecreasing_start_times" for box in rules.checkbox)
    checkbox.set_value(enabled).run()
    payloads = {}
    original = st.download_button

    def capture(label, data, *args, **kwargs):
        payloads[label] = data
        return original(label, data, *args, **kwargs)

    with patch("streamlit.download_button", side_effect=capture):
        generate(at)
    _, result, _, settings = app_solution(at)
    assert len(result.assignments) == 4
    assert settings.nondecreasing_start_times is enabled
    assert json.loads(payloads["作成条件を保存（JSON）"])["前日より早い出勤を禁止"] is enabled


def test_app_checkbox_change_invalidates_fingerprint_and_hides_downloads_both_directions():
    at = small_app()
    old_fingerprint = app_solution(generate(at))[0]
    for enabled in (True, False):
        at.checkbox(key="nondecreasing_start_times").set_value(enabled).run()
        assert any("入力条件が変更" in warning.value for warning in at.warning)
        no_app_results(at)
        new_fingerprint, _, _, settings = app_solution(generate(at))
        assert new_fingerprint != old_fingerprint
        assert settings.nondecreasing_start_times is enabled
        old_fingerprint = new_fingerprint


def test_app_weekly_table_forces_late_then_early_conflict_without_relaxation():
    at = small_app(size=7, pattern="三交代")
    # 準夜16〜24時→日勤08時は8時間休息。休息設定を8にして新ルールだけを分離。
    next(n for n in at.number_input if n.label == "勤務間の最低休息（時間）").set_value(8)
    at.session_state.weekly_source = pd.DataFrame([
        {"職員ID": "S01", "曜日": "月", "シフト": "準夜"},
        {"職員ID": "S01", "曜日": "火", "シフト": "日勤"},
    ])
    at.session_state.weekly_revision += 1
    at.run()
    _, result, staff, _ = app_solution(generate(at))
    expected = {Assignment("S01", MONDAY, "準夜"),
                Assignment("S01", MONDAY + timedelta(days=1), "日勤")}
    assert expected <= set(result.assignments)
    assert next(p for p in staff if p.employee_id == "S01").weekly_assignments == (
        WeeklyAssignment(0, "準夜"), WeeklyAssignment(1, "日勤"))
    at.checkbox(key="nondecreasing_start_times").check().run()
    no_app_results(at)
    generate(at)
    assert at.session_state.schedule[1].status == "infeasible"
    assert at.session_state.schedule[3].nondecreasing_start_times is True
    no_app_results(at)


@pytest.mark.parametrize("second_start,allowed", [("09:59", False), ("10:00", True), ("10:01", True)])
def test_app_uses_same_category_date_specific_override_start_times(second_start, allowed):
    at = small_app()
    at.session_state.weekly_source = pd.DataFrame([
        {"職員ID": "S01", "曜日": weekday, "シフト": "日勤"} for weekday in ("月", "火")
    ])
    at.session_state.weekly_revision += 1
    at.session_state.work_time_source = pd.DataFrame([
        {"職員ID": "S01", "日付": "", "シフト": "日勤", "開始": "10:00",
         "終了": "18:00", "翌日終了": False, "休憩（分）": 60},
        {"職員ID": "S01", "日付": "2026-01-27", "シフト": "日勤", "開始": second_start,
         "終了": "18:00", "翌日終了": False, "休憩（分）": 60},
    ])
    at.session_state.condition_revision += 1
    at.run()
    _, result, _, _ = app_solution(generate(at))
    assert len([a for a in result.assignments if a.employee_id == "S01"]) == 2
    at.checkbox(key="nondecreasing_start_times").check().run()
    no_app_results(at)
    generate(at)
    if allowed:
        _, _, staff, settings = app_solution(at)
        person = next(p for p in staff if p.employee_id == "S01")
        assert len(person.work_time_overrides) == 2
        assert employee_shift_time(person, settings, settings.dates[1], "日勤").start_minute == (
            int(second_start[:2]) * 60 + int(second_start[3:]))
    else:
        assert at.session_state.schedule[1].status == "infeasible"
        no_app_results(at)