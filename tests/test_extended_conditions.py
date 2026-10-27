"""勤務形態、専従勤務、月間労働時間、休憩のモデル回帰テスト。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from optimizer import Assignment, Employee, Settings, ShiftTime, audit_schedule, month_groups, required_working_minutes, solve_schedule


def configuration(**changes):
    return replace(Settings(date(2026, 1, 1), days=2, time_limit_seconds=5,
                            shift_names=("日勤",), shift_times=(ShiftTime(540, 1080, 60),)), **changes)


def people(size=4):
    return [Employee(str(i), f"職員{i}", True) for i in range(size)]


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_variable_shift_count(count):
    names = tuple(f"勤務{i}" for i in range(count))
    settings = configuration(days=1, shift_names=names, shift_times=(ShiftTime(480, 960),) * count)
    staff = people(count * 2)
    result = solve_schedule(staff, settings)
    assert result.has_solution, result.messages
    assert len(result.assignments) == count * 2
    assert set(a.shift for a in result.assignments) == set(names)
    assert not audit_schedule(staff, settings, result.assignments)


def test_night_only_staff_never_gets_day_shift():
    settings = configuration(days=3, shift_names=("日勤", "夜勤"),
                             shift_times=(ShiftTime(480, 1200, 60), ShiftTime(1200, 1920, 120)))
    staff = [replace(p, allowed_shifts=frozenset({"日勤" if i < 4 else "夜勤"})) for i, p in enumerate(people(8))]
    result = solve_schedule(staff, settings)
    assert result.has_solution, result.messages
    assert all(a.shift in staff[int(a.employee_id)].allowed_shifts for a in result.assignments)
    assert not audit_schedule(staff, settings, result.assignments)


def test_fixed_shift_shortage_is_reported():
    settings = configuration(shift_names=("日勤", "夜勤"), shift_times=(ShiftTime(480, 960), ShiftTime(960, 1440)))
    staff = [replace(p, allowed_shifts=frozenset({"日勤"})) for p in people(6)]
    result = solve_schedule(staff, settings)
    assert result.status == "infeasible"
    assert any("夜勤" in message and "勤務可能者は0人" in message for message in result.messages)


def test_break_deducted_for_hard_monthly_limit():
    settings = configuration(days=1)
    staff = [replace(p, max_monthly_minutes=480) for p in people(2)]
    result = solve_schedule(staff, settings)
    assert result.has_solution  # 拘束9時間、休憩1時間、実労働8時間。
    assert not audit_schedule(staff, settings, result.assignments)
    settings = replace(settings, shift_times=(ShiftTime(540, 1080, 59),))
    assert solve_schedule(staff, settings).status == "infeasible"


def test_monthly_cap_not_just_total_period_cap():
    settings = configuration(start_date=date(2026, 1, 31))
    staff = [replace(p, max_monthly_minutes=480) for p in people(2)]
    result = solve_schedule(staff, settings)
    assert result.has_solution, result.messages
    assert len(result.assignments) == 4  # 各人1月8h＋2月8h。
    assert not audit_schedule(staff, settings, result.assignments)


def test_zero_cap_is_work_prohibition_not_unset():
    staff = [replace(p, max_monthly_minutes=0) for p in people(2)]
    assert solve_schedule(staff, configuration()).status == "infeasible"


def test_required_working_time_can_require_above_minimum_staffing():
    # 1月1日だけを計画。月248時間なら日割り8時間、各人1勤務が必須。
    staff = [replace(p, monthly_working_minutes=248 * 60) for p in people(3)]
    settings = configuration(days=1)
    result = solve_schedule(staff, settings)
    assert result.status == "optimal"
    assert len(result.assignments) == 3  # 最低人数2人を超えても指定時間と完全一致。
    assert {a.employee_id for a in result.assignments} == {p.employee_id for p in staff}
    assert not audit_schedule(staff, settings, result.assignments)


def test_unset_working_time_adds_no_time_constraint():
    staff = people(3)
    settings = configuration(days=1)
    assert all(p.monthly_working_minutes is None for p in staff)
    assert required_working_minutes(staff[0], settings, (0,)) is None
    result = solve_schedule(staff, settings)
    assert result.status == "optimal"
    assert len(result.assignments) == 2
    assert not audit_schedule(staff, settings, result.assignments)


def test_zero_working_time_is_work_prohibition_not_unset():
    staff = [replace(p, monthly_working_minutes=0) for p in people(2)]
    assert solve_schedule(staff, configuration(days=1)).status == "infeasible"


def test_zero_working_time_employee_remains_unassigned():
    staff = people(3)
    staff[0] = replace(staff[0], monthly_working_minutes=0)
    result = solve_schedule(staff, configuration(days=1))
    assert result.has_solution
    assert len(result.assignments) == 2
    assert all(a.employee_id != "0" for a in result.assignments)
    assert not audit_schedule(staff, configuration(days=1), result.assignments)


def test_exact_working_time_and_upper_limit_both_enforced():
    staff = [replace(p, monthly_working_minutes=248 * 60, max_monthly_minutes=248 * 60)
             for p in people(2)]
    settings = configuration(days=1)
    result = solve_schedule(staff, settings)
    assert result.has_solution
    assert not audit_schedule(staff, settings, result.assignments)
    impossible = [replace(p, monthly_working_minutes=240 * 60) for p in staff]
    assert solve_schedule(impossible, settings).status == "infeasible"  # 日割りの整数分と8hが一致しない。


def test_required_working_time_matches_after_break_deduction():
    settings = configuration(days=1)
    staff = [replace(p, monthly_working_minutes=248 * 60) for p in people(2)]
    result = solve_schedule(staff, settings)
    assert result.has_solution, result.messages
    assert len(result.assignments) == 2
    assert settings.shift_times[0].duration_minutes == 540
    assert settings.shift_times[0].working_minutes == 480
    assert not audit_schedule(staff, settings, result.assignments)
    # 休憩が1分短いと実労働481分となり、指定480分には一致できない。
    changed = replace(settings, shift_times=(ShiftTime(540, 1080, 59),))
    assert solve_schedule(staff, changed).status == "infeasible"


@pytest.mark.parametrize("monthly_hours", [240, 256])
def test_required_working_time_never_allows_under_or_over_assignment(monthly_hours):
    staff = [replace(p, monthly_working_minutes=monthly_hours * 60) for p in people(2)]
    assert solve_schedule(staff, configuration(days=1)).status == "infeasible"


def test_partial_working_times_use_each_calendar_month_and_leap_year():
    settings = configuration(start_date=date(2028, 2, 29), days=2)
    person = replace(people(1)[0], monthly_working_minutes=160 * 60)
    groups = month_groups(settings)
    assert set(groups) == {"2028-02", "2028-03"}
    assert required_working_minutes(person, settings, groups["2028-02"]) == 331
    assert required_working_minutes(person, settings, groups["2028-03"]) == 310
    full_month = configuration(days=31)
    assert required_working_minutes(person, full_month, tuple(range(31))) == 9600


def test_month_crossing_shift_is_counted_in_start_month():
    settings = configuration(start_date=date(2026, 1, 31), days=1,
                             shift_times=(ShiftTime(1200, 1920, 120),))
    staff = [replace(p, max_monthly_minutes=600) for p in people(2)]
    result = solve_schedule(staff, settings)
    assert result.has_solution
    assert not audit_schedule(staff, settings, result.assignments)
    staff = [replace(p, max_monthly_minutes=599) for p in staff]
    assert solve_schedule(staff, settings).status == "infeasible"


def test_required_working_time_counts_entire_overnight_shift_in_start_month():
    settings = configuration(start_date=date(2026, 1, 31), days=1,
                             shift_times=(ShiftTime(1200, 1920, 120),))
    staff = [replace(p, monthly_working_minutes=310 * 60) for p in people(2)]
    assert required_working_minutes(staff[0], settings, (0,)) == 600
    result = solve_schedule(staff, settings)
    assert result.has_solution, result.messages
    assert len(result.assignments) == 2
    assert not audit_schedule(staff, settings, result.assignments)


@pytest.mark.parametrize("change", [
    {"allowed_shifts": frozenset({"不存在"})}, {"allowed_shifts": frozenset()},
    {"monthly_working_minutes": -1}, {"max_monthly_minutes": -1},
    {"max_monthly_minutes": 744 * 60 + 1}, {"monthly_working_minutes": 1.5},
    {"monthly_working_minutes": 744 * 60 + 1}, {"monthly_working_minutes": True},
    {"monthly_working_minutes": 481, "max_monthly_minutes": 480},
])
def test_invalid_employee_conditions(change):
    staff = people()
    staff[0] = replace(staff[0], **change)
    assert solve_schedule(staff, configuration()).status == "invalid"


@pytest.mark.parametrize("break_minutes", [-1, 540, 541, 1.5])
def test_invalid_break(break_minutes):
    settings = configuration(shift_times=(ShiftTime(540, 1080, break_minutes),))
    assert solve_schedule(people(), settings).status == "invalid"


@pytest.mark.parametrize("names", [(), ("同名", "同名"), ("",), ("休",), ("制限なし",)])
def test_invalid_shift_names(names):
    settings = configuration(shift_names=names, shift_times=(ShiftTime(540, 1080),) * len(names))
    assert solve_schedule(people(), settings).status == "invalid"


def test_audit_independently_detects_fixed_shift_cap_and_exact_working_time():
    settings = configuration(days=1, shift_names=("日勤", "夜勤"),
                             shift_times=(ShiftTime(480, 960), ShiftTime(960, 1440)))
    staff = [replace(people(1)[0], allowed_shifts=frozenset({"夜勤"}), max_monthly_minutes=0,
                     monthly_working_minutes=0)]
    errors = audit_schedule(staff, settings, (Assignment("0", settings.start_date, "日勤"),))
    assert any("許可シフト以外" in error for error in errors)
    assert any("上限を超え" in error for error in errors)
    assert any("指定" in error and "一致" in error for error in errors)


def test_audit_rejects_missing_required_working_time_even_with_sufficient_staffing():
    settings = configuration(days=1)
    staff = [replace(p, monthly_working_minutes=248 * 60) for p in people(3)]
    assignments = tuple(Assignment(p.employee_id, settings.start_date, "日勤") for p in staff[:2])
    errors = audit_schedule(staff, settings, assignments)
    assert any(staff[2].name in error and "一致" in error for error in errors)


@pytest.mark.parametrize("break_minutes", [59, 61])
def test_audit_rejects_even_one_minute_working_time_difference(break_minutes):
    settings = configuration(days=1, shift_times=(ShiftTime(540, 1080, break_minutes),))
    staff = [replace(p, monthly_working_minutes=248 * 60) for p in people(2)]
    assignments = tuple(Assignment(p.employee_id, settings.start_date, "日勤") for p in staff)
    errors = audit_schedule(staff, settings, assignments)
    for person in staff:
        assert any(person.name in error and "一致" in error for error in errors)


def test_multiple_days_off_and_fixed_shifts_combined():
    settings = configuration(days=4)
    staff = people(6)
    staff[0] = replace(staff[0], days_off=frozenset({settings.start_date, settings.start_date + timedelta(days=2)}),
                       allowed_shifts=frozenset({"日勤"}), max_monthly_minutes=480)
    result = solve_schedule(staff, settings)
    assert result.has_solution
    assert not audit_schedule(staff, settings, result.assignments)