from dataclasses import replace
from datetime import date, timedelta

import pytest

from optimizer import (
    Assignment, Employee, Settings, ShiftTime, SHIFTS, audit_schedule, solve_schedule,
)


START = date(2026, 11, 1)


def team(size=15, leaders=6):
    return [Employee(f"S{i:02}", f"職員{i}", i < leaders) for i in range(size)]


def config(**changes):
    return replace(Settings(START, days=7, time_limit_seconds=5), **changes)


def test_all_constraints_and_requested_days_off():
    employees = team()
    employees[0] = replace(employees[0], days_off=frozenset({START, START + timedelta(days=3)}))
    settings = config()
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert audit_schedule(employees, settings, result.assignments) == []
    assert not any(a.employee_id == "S00" and a.day in employees[0].days_off for a in result.assignments)
    assert len(result.assignments) == settings.days * settings.minimum_staff * 3


def test_each_day_mode_allows_single_leader():
    employees = team(size=6, leaders=1)
    settings = config(days=2, leader_mode="each_day", minimum_rest_hours=0)
    result = solve_schedule(employees, settings)
    assert result.has_solution
    assert audit_schedule(employees, settings, result.assignments) == []


def test_each_shift_mode_requires_three_leaders():
    result = solve_schedule(team(size=9, leaders=2), config())
    assert result.status == "infeasible"
    assert result.assignments == ()
    assert any("主任以上は2人" in message for message in result.messages)


def test_days_off_create_daily_staff_shortage():
    employees = team(size=6, leaders=3)
    employees[0] = replace(employees[0], days_off=frozenset({START}))
    result = solve_schedule(employees, config(days=1))
    assert result.status == "infeasible"
    assert any("勤務可能者は5人" in message for message in result.messages)


def test_consecutive_work_infeasible_without_daily_shortage():
    # 毎日の人数は十分でも、全員が毎日必要だと連続勤務上限を守れない。
    result = solve_schedule(team(size=6, leaders=3), config(days=3, max_consecutive_days=2))
    assert result.status == "infeasible"
    assert not result.has_solution


@pytest.mark.parametrize("changes", [
    {"minimum_staff": 1}, {"minimum_staff": 2.5}, {"days": 0},
    {"days": 63}, {"leader_mode": "invalid"}, {"max_consecutive_days": 0},
    {"minimum_rest_hours": -1}, {"time_limit_seconds": 0},
])
def test_invalid_settings(changes):
    assert solve_schedule(team(), config(**changes)).status == "invalid"


def test_duplicate_ids_rejected_but_duplicate_names_allowed():
    employees = team()
    employees[1] = replace(employees[1], employee_id=employees[0].employee_id)
    assert solve_schedule(employees, config()).status == "invalid"
    employees[1] = replace(employees[1], employee_id="unique", name=employees[0].name)
    assert solve_schedule(employees, config(days=1)).has_solution


def test_empty_staff_rejected():
    assert solve_schedule([], config()).status == "invalid"


def test_zero_available_leaders_rejected():
    assert solve_schedule(team(leaders=0), config(leader_mode="each_day")).status == "infeasible"


def test_audit_detects_short_rest_and_requested_day_off():
    employees = [Employee("A", "職員A", True, frozenset({START}))]
    settings = config(days=2)
    assignments = (Assignment("A", START, "準夜"), Assignment("A", START + timedelta(days=1), "深夜"))
    errors = audit_schedule(employees, settings, assignments)
    assert "休み希望日に勤務があります。" in errors
    assert any("休息が不足" in message for message in errors)


def test_audit_detects_duplicate_daily_work_and_missing_leader():
    employees = team(size=6, leaders=0)
    assignments = tuple(Assignment(person.employee_id, START, shift) for person in employees for shift in SHIFTS)
    errors = audit_schedule(employees, config(days=1), assignments)
    assert any("1日2勤務" in message for message in errors)
    assert any("主任以上が不在" in message for message in errors)


def test_higher_minimum_staff():
    employees = team(size=12, leaders=4)
    settings = config(days=1, minimum_staff=3)
    result = solve_schedule(employees, settings)
    assert result.has_solution
    assert len(result.assignments) == 9
    assert audit_schedule(employees, settings, result.assignments) == []


@pytest.mark.parametrize("minimum_rest_hours", [0, 8, 11, 24])
def test_rest_constraints_independently_audited(minimum_rest_hours):
    employees = team()
    settings = config(days=4, max_consecutive_days=2, minimum_rest_hours=minimum_rest_hours)
    result = solve_schedule(employees, settings)
    assert result.has_solution
    assert audit_schedule(employees, settings, result.assignments) == []


@pytest.mark.parametrize("timings", [
    (ShiftTime(0, 0), ShiftTime(480, 960), ShiftTime(960, 1440)),
    (ShiftTime(480, 400), ShiftTime(480, 960), ShiftTime(960, 1440)),
    (ShiftTime(-1, 480), ShiftTime(480, 960), ShiftTime(960, 1440)),
    (ShiftTime(1440, 1500), ShiftTime(480, 960), ShiftTime(960, 1440)),
    (ShiftTime(0, 1441), ShiftTime(480, 960), ShiftTime(960, 1440)),
    (ShiftTime(0, 480),),
])
def test_invalid_shift_times(timings):
    result = solve_schedule(team(), config(shift_times=timings))
    assert result.status == "invalid"
    assert not result.assignments


def test_minute_precision_at_rest_boundary():
    assignments = (Assignment("A", START, "準夜"), Assignment("A", START + timedelta(days=1), "日勤"))
    employees = [Employee("A", "職員A", True)]
    # 21:31終了→翌08:30開始は10時間59分。21:30なら11時間。
    timings = (ShiftTime(0, 480), ShiftTime(510, 990), ShiftTime(990, 1291))
    errors = audit_schedule(employees, config(days=2, shift_times=timings), assignments)
    assert any("休息が不足" in message for message in errors)
    timings = (*timings[:2], ShiftTime(990, 1290))
    errors = audit_schedule(employees, config(days=2, shift_times=timings), assignments)
    assert not any("休息が不足" in message for message in errors)


def test_overnight_rest_checked_even_with_day_between():
    timings = (ShiftTime(0, 480), ShiftTime(480, 960), ShiftTime(1380, 2820))
    assignments = (Assignment("A", START, "準夜"), Assignment("A", START + timedelta(days=2), "深夜"))
    errors = audit_schedule([Employee("A", "職員A", True)], config(days=3, shift_times=timings), assignments)
    assert any("休息が不足" in message for message in errors)


@pytest.mark.parametrize("end_minute,violates", [(1440, False), (1441, True), (1920, True)])
def test_requested_day_off_includes_overnight_work(end_minute, violates):
    timings = (ShiftTime(0, 480), ShiftTime(480, 960), ShiftTime(960, end_minute))
    employees = [Employee("A", "職員A", True, frozenset({START + timedelta(days=1)}))]
    errors = audit_schedule(employees, config(days=1, shift_times=timings), (Assignment("A", START, "準夜"),))
    assert ("休み希望日に勤務があります。" in errors) is violates


def test_overnight_solver_respects_calendar_days_off_and_real_rest():
    from datetime import datetime, time

    timings = (ShiftTime(0, 465), ShiftTime(465, 990), ShiftTime(990, 1470))
    employees = team()
    off = START + timedelta(days=2)
    employees[0] = replace(employees[0], days_off=frozenset({off}))
    settings = config(days=5, shift_times=timings)
    result = solve_schedule(employees, settings)
    assert result.has_solution, result.messages
    assert audit_schedule(employees, settings, result.assignments) == []
    for person in employees:
        intervals = []
        for assignment in result.assignments:
            if assignment.employee_id != person.employee_id:
                continue
            timing = timings[SHIFTS.index(assignment.shift)]
            midnight = datetime.combine(assignment.day, time())
            begin, end = midnight + timedelta(minutes=timing.start_minute), midnight + timedelta(minutes=timing.end_minute)
            intervals.append((begin, end))
            for requested in person.days_off:
                off_start = datetime.combine(requested, time())
                assert end <= off_start or begin >= off_start + timedelta(days=1)
        intervals.sort()
        assert all(b[0] - a[1] >= timedelta(hours=settings.minimum_rest_hours) for a, b in zip(intervals, intervals[1:]))


def test_overnight_before_requested_day_off_can_make_schedule_infeasible():
    employees = [replace(person, days_off=frozenset({START + timedelta(days=1)})) for person in team(size=6, leaders=3)]
    timings = (ShiftTime(0, 480), ShiftTime(480, 960), ShiftTime(960, 1500))
    # 人数の事前チェックは通るが全員の翌日が希望休のため準夜を置けない。
    result = solve_schedule(employees, config(days=1, shift_times=timings))
    assert result.status == "infeasible"


def test_time_label_distinguishes_next_day():
    assert ShiftTime(960, 1440).label == "16:00–翌日 00:00"
    assert ShiftTime(1385, 1905).label == "23:05–翌日 07:45"