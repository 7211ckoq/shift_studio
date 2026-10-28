from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest

from optimizer import (
    Assignment, Employee, Settings, ShiftMerge, ShiftTime, WeeklyAssignment, WorkTimeOverride,
    audit_schedule, avoided_shift_assignments, day_shift_names, employee_shift_time,
    shift_base_time, shift_label, solve_schedule, validate_inputs,
)


START = date(2026, 11, 2)  # 月曜
NAMES = ("早番①", "早番②", "中番", "遅番")
TIMES = (ShiftTime(420, 720), ShiftTime(720, 960, 30), ShiftTime(600, 1140, 60), ShiftTime(960, 1320))


def team(size=12):
    return [Employee(f"S{i:02}", f"職員{i}", True) for i in range(size)]


def config(**changes):
    base = Settings(START, days=3, minimum_staff=1, minimum_rest_hours=0, time_limit_seconds=10,
                    shift_times=TIMES, shift_names=NAMES)
    return replace(base, **changes)


def test_minimum_staff_one_allows_one_person_operation():
    settings = config()
    employees = team(4)
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert audit_schedule(employees, settings, result.assignments) == []
    assert len(result.assignments) == settings.days * len(NAMES)


def test_two_shifts_merged_on_selected_day_only():
    merge = ShiftMerge(START, ("早番②", "早番①"))
    settings = config(shift_merges=(merge,))
    employees = team()
    assert day_shift_names(settings, START) == ("早番①", "中番", "遅番")
    assert day_shift_names(settings, START + timedelta(days=1)) == NAMES
    assert shift_label(settings, START, "早番①") == "早番①+早番②"
    assert shift_base_time(settings, START, "早番①") == ShiftTime(420, 960, 30)
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert audit_schedule(employees, settings, result.assignments) == []
    first_day = [a for a in result.assignments if a.day == START]
    assert sorted(a.shift for a in first_day) == ["中番", "早番①", "遅番"]
    assert len(result.assignments) == 3 + 4 + 4


def test_three_shifts_merged_and_working_time_uses_merged_span():
    merge = ShiftMerge(START, ("早番①", "早番②", "中番"))
    settings = config(days=1, shift_merges=(merge,))
    employees = team(4)
    assert day_shift_names(settings, START) == ("早番①", "遅番")
    timing = shift_base_time(settings, START, "早番①")
    assert (timing.start_minute, timing.end_minute, timing.break_minutes) == (420, 1140, 90)
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    worker = next(a for a in result.assignments if a.shift == "早番①")
    person = next(p for p in employees if p.employee_id == worker.employee_id)
    assert employee_shift_time(person, settings, START, "早番①").working_minutes == 720 - 90


def test_two_separate_merges_on_same_day_and_break_override():
    merges = (ShiftMerge(START, ("早番①", "早番②"), 45), ShiftMerge(START, ("中番", "遅番")))
    settings = config(days=1, shift_merges=merges)
    assert day_shift_names(settings, START) == ("早番①", "中番")
    assert shift_base_time(settings, START, "早番①").break_minutes == 45
    assert shift_base_time(settings, START, "中番") == ShiftTime(600, 1320, 60)
    result = solve_schedule(team(4), settings)
    assert result.has_solution, result.messages
    assert len(result.assignments) == 2


def test_audit_rejects_assignment_to_absorbed_shift():
    settings = config(days=1, shift_merges=(ShiftMerge(START, ("早番①", "早番②")),))
    employees = team(4)
    assignments = (
        Assignment("S00", START, "早番①"), Assignment("S01", START, "早番②"),
        Assignment("S02", START, "中番"), Assignment("S03", START, "遅番"),
    )
    assert any("配置対象外" in error for error in audit_schedule(employees, settings, assignments))


@pytest.mark.parametrize("merge, message", [
    (ShiftMerge(START, ("早番①",)), "2つ以上"),
    (ShiftMerge(START, ("早番①", "早番①")), "2つ以上"),
    (ShiftMerge(START, ("早番①", "夜勤")), "2つ以上"),
    (ShiftMerge(START + timedelta(days=10), ("早番①", "早番②")), "作成期間内"),
    (ShiftMerge(START, ("早番①", "遅番")), "途切れ"),
    (ShiftMerge(START, ("早番①", "早番②"), 600), "休憩"),
])
def test_invalid_merges(merge, message):
    errors = validate_inputs(team(), config(shift_merges=(merge,)))
    assert any(message in error for error in errors), errors


def test_overlapping_merges_on_same_day_rejected():
    merges = (ShiftMerge(START, ("早番①", "早番②")), ShiftMerge(START, ("早番②", "中番")))
    errors = validate_inputs(team(), config(shift_merges=merges))
    assert any("重複" in error for error in errors)


def test_fixed_weekly_and_avoided_shifts_follow_merge():
    settings = config(days=1, shift_merges=(ShiftMerge(START, ("早番①", "早番②")),))
    employees = team(4)
    employees[0] = replace(employees[0], allowed_shifts=frozenset({"早番②"}),
                           weekly_assignments=(WeeklyAssignment(START.weekday(), "早番②"),))
    employees[1] = replace(employees[1], avoided_shifts=frozenset({"早番②"}), shift_avoidance_weight=50)
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert Assignment("S00", START, "早番①") in result.assignments
    avoided = avoided_shift_assignments(employees, (Assignment("S01", START, "早番①"),), settings)
    assert avoided == (Assignment("S01", START, "早番①"),)
    assert avoided_shift_assignments(employees, (Assignment("S01", START, "早番①"),)) == ()


def test_recurring_override_ignored_and_dated_override_on_absorbed_shift_rejected():
    settings = config(days=2, shift_merges=(ShiftMerge(START, ("早番①", "早番②")),))
    short = ShiftTime(480, 720)
    person = replace(team(1)[0], work_time_overrides=(WorkTimeOverride("早番①", short),))
    assert employee_shift_time(person, settings, START, "早番①") == ShiftTime(420, 960, 30)
    assert employee_shift_time(person, settings, START + timedelta(days=1), "早番①") == short
    dated = replace(person, work_time_overrides=(WorkTimeOverride("早番②", short, START),))
    errors = validate_inputs([dated, *team(3)[1:]], settings)
    assert any("統合されています" in error for error in errors)


def test_merge_reduces_capacity_requirement():
    # 3人では4シフトを1人ずつ埋められないが、統合日なら3勤務で足りる。
    employees = team(3)
    assert solve_schedule(employees, config(days=1)).status == "infeasible"
    result = solve_schedule(employees, config(days=1, shift_merges=(ShiftMerge(START, ("早番①", "早番②")),)))
    assert result.has_solution, result.messages


def test_parse_shift_merges_from_editor_and_csv():
    from app import merges_csv_frame, parse_shift_merges, shift_merges_frame
    editor = pd.DataFrame([
        {"日付": "2026-11-02", "統合するシフト": ["早番①", "早番②"], "休憩（分）": None},
        {"日付": None, "統合するシフト": [], "休憩（分）": None},
    ])
    merges = parse_shift_merges(editor)
    assert merges == (ShiftMerge(START, ("早番①", "早番②")),)
    csv_frame = merges_csv_frame(editor)
    assert csv_frame.iloc[0]["統合するシフト"] == '["早番①", "早番②"]'
    assert parse_shift_merges(csv_frame.astype(str).replace("None", "")) == merges
    assert shift_merges_frame(merges).iloc[0]["統合するシフト"] == ["早番①", "早番②"]
    with pytest.raises(ValueError, match="2つ以上"):
        parse_shift_merges(pd.DataFrame([{"日付": "2026-11-02", "統合するシフト": ["早番①"], "休憩（分）": ""}]))
