"""曜日固定・勤務帯回避の必須条件、辞書式目的関数、公開APIの回帰テスト。"""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, timedelta
from inspect import Parameter, signature

import pytest

from optimizer import (
    Assignment, AvoidPair, Employee, RoleRequirement, Settings, ShiftTime,
    WeeklyAssignment, WorkTimeOverride, audit_schedule, avoided_shift_assignments,
    pair_conflicts, solve_schedule, validate_inputs,
)


MONDAY = date(2026, 1, 26)
SHIFT = "外来α"
BASE = ShiftTime(480, 960)


def config(**changes):
    return replace(Settings(MONDAY, days=1, shift_names=(SHIFT,), shift_times=(BASE,),
                            time_limit_seconds=5), **changes)


def team(size=4):
    return [Employee(str(i), f"職員{i}", True) for i in range(size)]


def fixed(person, *entries):
    return replace(person, weekly_assignments=tuple(WeeklyAssignment(*entry) for entry in entries))


def solved(staff, settings):
    result = solve_schedule(staff, settings)
    assert result.status == "optimal", result.messages
    assert audit_schedule(staff, settings, result.assignments) == []
    return result.assignments


def impossible(staff, settings):
    assert validate_inputs(staff, settings) == []
    result = solve_schedule(staff, settings)
    assert result.status == "infeasible", result.messages
    assert any("曜日固定" in message for message in result.messages)


def test_exact_api_frozen_defaults_and_positional_backwards_compatibility():
    assert [f.name for f in fields(WeeklyAssignment)] == ["weekday", "shift"]
    assert str(signature(WeeklyAssignment)) == "(weekday: int, shift: str) -> None"
    person = Employee("a", "氏名", True, frozenset(), None, None, None,
                      (WorkTimeOverride(SHIFT, BASE),))
    assert person.weekly_assignments == ()
    assert person.avoided_shifts == frozenset()
    assert person.shift_avoidance_weight == 1
    assert person.work_time_overrides == (WorkTimeOverride(SHIFT, BASE),)
    assert fields(Employee)[-1].name == "work_time_overrides"
    for name in ("weekly_assignments", "avoided_shifts", "shift_avoidance_weight"):
        assert signature(Employee).parameters[name].kind is Parameter.KEYWORD_ONLY
    assert [f.name for f in fields(Settings)] == [
        "start_date", "days", "minimum_staff", "leader_mode", "max_consecutive_days",
        "minimum_rest_hours", "time_limit_seconds", "shift_times", "shift_names",
        "role_requirements", "nondecreasing_start_times", "avoid_pairs",
    ]
    with pytest.raises(FrozenInstanceError):
        setattr(WeeklyAssignment(0, SHIFT), "weekday", 1)
    with pytest.raises(FrozenInstanceError):
        setattr(person, "shift_avoidance_weight", 2)


@pytest.mark.parametrize("weekday", range(7))
def test_recurring_start_weekday_across_month_boundary(weekday):
    settings = config(days=14, max_consecutive_days=14)
    staff = team()
    staff[0] = fixed(staff[0], (weekday, SHIFT))
    output = solved(staff, settings)
    required = {day for day in settings.dates if day.weekday() == weekday}
    assert len(required) == 2
    assert {Assignment("0", day, SHIFT) for day in required}.issubset(output)
    assert {day.month for day in settings.dates} == {1, 2}


def test_weekly_fixed_forces_staffing_above_minimum():
    staff = [fixed(p, (0, SHIFT)) for p in team(3)]
    assert len(solved(staff, config())) == 3


def test_nonmatching_weekday_does_not_force_work_or_another_shift():
    staff = team(3)
    staff[0] = replace(fixed(staff[0], (1, SHIFT)), monthly_working_minutes=0)
    output = solved(staff, config())
    assert {a.employee_id for a in output} == {"1", "2"}


def test_weekly_fixed_does_not_force_days_off_on_other_weekdays():
    staff = team(2)
    staff[0] = fixed(staff[0], (0, SHIFT))
    output = solved(staff, config(days=2))
    assert Assignment("0", MONDAY + timedelta(days=1), SHIFT) in output


def test_weekly_fixed_does_not_restrict_other_weekdays_to_same_shift():
    settings = config(days=2, shift_names=(SHIFT, "夕診β"), shift_times=(BASE, BASE))
    staff = team(5)
    staff[0] = fixed(staff[0], (0, SHIFT))
    # 火曜のαは1,2だけ、βは0,3だけ。0の火曜β勤務を必須にする。
    staff[1:3] = [replace(p, allowed_shifts=frozenset({SHIFT})) for p in staff[1:3]]
    staff[3] = replace(staff[3], allowed_shifts=frozenset({"夕診β"}))
    staff[4] = replace(staff[4], days_off=frozenset({MONDAY + timedelta(days=1)}))
    output = solved(staff, settings)
    assert Assignment("0", MONDAY, SHIFT) in output
    assert Assignment("0", MONDAY + timedelta(days=1), "夕診β") in output


@pytest.mark.parametrize("change", [
    {"days_off": frozenset({MONDAY})},
    {"allowed_shifts": frozenset({"別勤務"})},
    {"max_monthly_minutes": 479},
    {"monthly_working_minutes": 0},
    {"monthly_working_minutes": 31 * 479},
])
def test_weekly_fixed_conflicts_are_infeasible_not_invalid(change):
    staff = team(6)
    staff[0] = replace(fixed(staff[0], (0, SHIFT)), **change)
    settings = config(shift_names=(SHIFT, "別勤務"), shift_times=(BASE, BASE))
    impossible(staff, settings)


@pytest.mark.parametrize("end,feasible", [(1440, True), (1441, False), (1920, False)])
def test_fixed_start_day_respects_next_calendar_day_off(end, feasible):
    settings = config(shift_times=(ShiftTime(1200, end),))
    staff = team()
    staff[0] = replace(fixed(staff[0], (0, SHIFT)),
                       days_off=frozenset({MONDAY + timedelta(days=1)}))
    if feasible:
        assert Assignment("0", MONDAY, SHIFT) in solved(staff, settings)
    else:
        impossible(staff, settings)


def test_weekly_fixed_cannot_relax_consecutive_limit():
    staff = team(6)
    staff[0] = fixed(staff[0], (0, SHIFT), (1, SHIFT))
    impossible(staff, config(days=2, max_consecutive_days=1))


def test_weekly_fixed_cannot_relax_rest():
    settings = config(days=2, shift_names=(SHIFT, "夜勤"),
                      shift_times=(BASE, ShiftTime(1200, 1920)))
    staff = team(8)
    staff[0] = fixed(staff[0], (0, "夜勤"), (1, SHIFT))
    impossible(staff, settings)


def test_weekly_fixed_and_override_check_rest_even_with_a_day_between():
    staff = team(6)
    staff[0] = replace(fixed(staff[0], (0, SHIFT), (2, SHIFT)),
                       work_time_overrides=(WorkTimeOverride(SHIFT, ShiftTime(1380, 2760), MONDAY),))
    impossible(staff, config(days=3))  # 月曜開始・火曜22時終了から水曜08時まで10時間。


@pytest.mark.parametrize("explicit_roles", [False, True])
def test_weekly_fixed_cannot_relax_role_coverage(explicit_roles):
    staff = [replace(p, is_leader=i == 0) for i, p in enumerate(team(6))]
    staff[0] = fixed(staff[0], (0, SHIFT))
    settings = config(shift_names=(SHIFT, "別勤務"), shift_times=(BASE, BASE))
    if explicit_roles:
        settings = replace(settings, role_requirements=(RoleRequirement("責任者", "each_shift"),))
        staff = [replace(p, roles=frozenset({"責任者"}) if i == 0 else frozenset())
                 for i, p in enumerate(staff)]
    impossible(staff, settings)


@pytest.mark.parametrize("dated_first", [False, True])
def test_weekly_fixed_uses_normal_override_precedence_breaks_and_exact_minutes(dated_first):
    overrides = (WorkTimeOverride(SHIFT, ShiftTime(420, 1020, 60)),
                 WorkTimeOverride(SHIFT, ShiftTime(420, 960, 60), MONDAY))
    staff = team()
    staff[0] = replace(fixed(staff[0], (0, SHIFT), (1, SHIFT)),
                       work_time_overrides=overrides[::-1] if dated_first else overrides,
                       monthly_working_minutes=31 * 510, max_monthly_minutes=31 * 510)
    output = solved(staff, config(days=2))
    assert sum(a.employee_id == "0" for a in output) == 2
    # 実勤務は480+540分。指定を1分/日変えても近似せず両立不可。
    staff[0] = replace(staff[0], monthly_working_minutes=31 * 509)
    impossible(staff, config(days=2))


def test_weekly_fixed_override_may_require_extra_coverage_staff():
    staff = team()
    staff[0] = replace(fixed(staff[0], (0, SHIFT)),
                       work_time_overrides=(WorkTimeOverride(SHIFT, ShiftTime(480, 720)),))
    output = solved(staff, config())
    assert Assignment("0", MONDAY, SHIFT) in output
    assert len(output) == 3


def test_weekly_fixed_and_avoided_same_shift_remains_a_solution_not_audit_error():
    staff = team(3)
    staff[0] = replace(fixed(staff[0], (0, SHIFT)), avoided_shifts=frozenset({SHIFT}),
                       shift_avoidance_weight=100)
    output = solved(staff, config())
    assert avoided_shift_assignments(staff, output) == (Assignment("0", MONDAY, SHIFT),)


@pytest.mark.parametrize("wrong", [None, "別勤務"])
def test_independent_audit_detects_omitted_or_wrong_weekly_fixed_shift(wrong):
    settings = config(shift_names=(SHIFT, "別勤務"), shift_times=(BASE, BASE))
    staff = team(5)
    staff[0] = fixed(staff[0], (0, SHIFT))
    output = (Assignment("1", MONDAY, SHIFT), Assignment("2", MONDAY, SHIFT),
              Assignment("3", MONDAY, "別勤務"), Assignment("4", MONDAY, "別勤務"))
    if wrong is not None:
        output += (Assignment("0", MONDAY, wrong),)
    errors = audit_schedule(staff, settings, output)
    assert len(errors) == 1
    assert "曜日固定" in errors[0] and SHIFT in errors[0] and staff[0].name in errors[0]


def test_audit_does_not_require_weekly_fixed_outside_period():
    staff = team(3)
    staff[0] = fixed(staff[0], (6, SHIFT))
    output = tuple(Assignment(p.employee_id, MONDAY, SHIFT) for p in staff[1:])
    assert audit_schedule(staff, config(), output) == []


@pytest.mark.parametrize("weekly", [
    None, [], [WeeklyAssignment(0, SHIFT)], "月曜", (None,), ((0, SHIFT),),
    (WeeklyAssignment(-1, SHIFT),), (WeeklyAssignment(7, SHIFT),),
    (WeeklyAssignment(True, SHIFT),), (WeeklyAssignment(False, SHIFT),),
    (WeeklyAssignment(0.0, SHIFT),), (WeeklyAssignment("0", SHIFT),),
    (WeeklyAssignment(None, SHIFT),), (WeeklyAssignment([], SHIFT),),
    (WeeklyAssignment(0, "未登録"),), (WeeklyAssignment(0, None),),
    (WeeklyAssignment(0, []),),
    (WeeklyAssignment(0, SHIFT), WeeklyAssignment(0, SHIFT)),
    (WeeklyAssignment(0, SHIFT), WeeklyAssignment(0, "別勤務")),
])
def test_invalid_weekly_input_types_names_and_duplicate_weekdays(weekly):
    staff = team()
    staff[0] = replace(staff[0], weekly_assignments=weekly)
    result = solve_schedule(staff, config())
    assert result.status == "invalid"
    assert any("曜日固定" in message for message in result.messages)


@pytest.mark.parametrize("avoided", [None, SHIFT, [SHIFT], (SHIFT,), {1},
                                     frozenset({1}), {"未登録"}, {SHIFT, "未登録"}])
def test_invalid_avoided_shift_input_types_and_unknown_names(avoided):
    staff = team()
    staff[0] = replace(staff[0], avoided_shifts=avoided)
    assert solve_schedule(staff, config()).status == "invalid"


@pytest.mark.parametrize("weight", [0, -1, 101, True, False, 1.0, "1", None])
@pytest.mark.parametrize("has_preferences", [False, True])
def test_weight_always_validated_even_without_preferences(weight, has_preferences):
    staff = team()
    staff[0] = replace(staff[0], shift_avoidance_weight=weight,
                       avoided_shifts=frozenset({SHIFT}) if has_preferences else frozenset())
    assert solve_schedule(staff, config()).status == "invalid"


@pytest.mark.parametrize("container", [set, frozenset])
@pytest.mark.parametrize("weight", [1, 100])
def test_valid_preference_containers_and_weight_boundaries(container, weight):
    staff = team(3)
    staff[0] = replace(staff[0], avoided_shifts=container({SHIFT}), shift_avoidance_weight=weight)
    assert avoided_shift_assignments(staff, solved(staff, config())) == ()


def test_soft_avoidance_dominates_fairness_but_does_not_ban_work():
    staff = team(3)
    staff[0] = replace(staff[0], avoided_shifts=frozenset({SHIFT}))
    output = solved(staff, config(days=2))
    assert {a.employee_id for a in output} == {"1", "2"}
    # 代わりがいなければ回避希望でも勤務し、独立検査は違反扱いしない。
    output = solved(staff[:2], config(days=2))
    assert len(avoided_shift_assignments(staff[:2], output)) == 2


def test_multiple_avoided_shifts_report_each_actual_assignment_once():
    settings = config(shift_names=(SHIFT, "別勤務"), shift_times=(BASE, BASE))
    staff = [replace(p, avoided_shifts=frozenset(settings.shift_names),
                     shift_avoidance_weight=100 if i == 0 else 1)
             for i, p in enumerate(team(5))]
    output = solved(staff, settings)
    assert len(output) == 4
    assert avoided_shift_assignments(staff, output) == output
    assert all(a.employee_id != "0" for a in output)


def test_preference_reporting_preserves_order_identity_and_does_not_invent_unknown_staff():
    staff = team(2)
    staff[0] = replace(staff[0], avoided_shifts=frozenset({SHIFT, "別勤務"}))
    output = (Assignment("0", MONDAY, "別勤務"), Assignment("1", MONDAY, SHIFT),
              Assignment("0", MONDAY, SHIFT), Assignment("未登録", MONDAY, SHIFT))
    reported = avoided_shift_assignments(staff, output)
    assert reported == (output[0], output[2])
    assert reported[0] is output[0]
    assert avoided_shift_assignments([], output) == ()
    assert avoided_shift_assignments(staff, ()) == ()


def test_placements_primary_even_if_extra_partial_staff_avoid_all_penalties():
    staff = team(6)
    staff[:2] = [replace(p, avoided_shifts=frozenset({SHIFT}), shift_avoidance_weight=100)
                 for p in staff[:2]]
    for i in range(2, 6):
        timing = ShiftTime(480, 720) if i < 4 else ShiftTime(720, 960)
        staff[i] = replace(staff[i], work_time_overrides=(WorkTimeOverride(SHIFT, timing),))
    # 2人の全日勤務（罰点200）対4人の半日勤務（罰点0）。配置数を必ず優先。
    output = solved(staff, config())
    assert {a.employee_id for a in output} == {"0", "1"}
    assert len(avoided_shift_assignments(staff, output)) == 2


@pytest.mark.parametrize("pair_weight,preference_weight,chosen", [(3, 2, "2"), (3, 4, "1"),
                                                                   (100, 99, "2"), (99, 100, "1")])
def test_pair_and_shift_penalties_combine_at_same_weight_ratio(pair_weight, preference_weight, chosen):
    staff = team(3)
    staff[0] = fixed(staff[0], (0, SHIFT))
    staff[2] = replace(staff[2], avoided_shifts=frozenset({SHIFT}),
                       shift_avoidance_weight=preference_weight)
    settings = config(avoid_pairs=(AvoidPair("0", "1", pair_weight),))
    output = solved(staff, settings)
    assert {a.employee_id for a in output} == {"0", chosen}
    assert len(pair_conflicts(output, settings)) == (chosen == "1")
    assert len(avoided_shift_assignments(staff, output)) == (chosen == "2")


def test_combined_penalty_is_sum_not_lexicographic_pair_then_shift():
    staff = team(4)
    staff[0] = fixed(staff[0], (0, SHIFT))
    staff[1] = replace(staff[1], avoided_shifts=frozenset({SHIFT}), shift_avoidance_weight=2)
    staff[2] = replace(staff[2], avoided_shifts=frozenset({SHIFT}), shift_avoidance_weight=4)
    staff[3] = replace(staff[3], monthly_working_minutes=0)
    settings = config(avoid_pairs=(AvoidPair("0", "1", 1),))
    output = solved(staff, settings)
    # ペア1＋勤務帯2＝3なので、ペア0＋勤務帯4より良い。
    assert {a.employee_id for a in output} == {"0", "1"}


def test_no_new_conditions_retains_legacy_solution_and_empty_report():
    staff = team()
    settings = config(days=3)
    output = solved(staff, settings)
    assert len(output) == settings.days * settings.minimum_staff
    assert avoided_shift_assignments(staff, output) == ()