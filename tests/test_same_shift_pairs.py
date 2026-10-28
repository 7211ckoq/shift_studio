"""新人・教育担当の同一シフト条件と独立検査の回帰テスト。"""

from dataclasses import replace
from datetime import date, timedelta
from inspect import Parameter, signature

import pytest

from optimizer import (
    Assignment, AvoidPair, Employee, SameShiftPair, Settings, ShiftTime,
    WeeklyAssignment, WorkTimeOverride, audit_schedule, same_shift_violations,
    solve_schedule, validate_inputs,
)


START = date(2026, 1, 26)
SHIFT = "外来α"


def config(pair=None, **changes):
    return replace(Settings(START, days=1, shift_names=(SHIFT,),
                            shift_times=(ShiftTime(480, 960),), time_limit_seconds=5,
                            same_shift_pairs=(pair,) if pair else ()), **changes)


def team(size=4):
    return [Employee(str(i), f"職員{i}", True) for i in range(size)]


def force(person, shift=SHIFT):
    return replace(person, weekly_assignments=(WeeklyAssignment(START.weekday(), shift),))


def solved(staff, settings):
    result = solve_schedule(staff, settings)
    assert result.status == "optimal", result.messages
    assert not audit_schedule(staff, settings, result.assignments)
    return result.assignments


def test_defaults_and_keyword_only_settings_preserve_positional_api():
    assert SameShiftPair("a", "b") == SameShiftPair("a", "b", "hard", "a_to_b", 1)
    assert Settings(START).same_shift_pairs == ()
    assert signature(Settings).parameters["same_shift_pairs"].kind is Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("mode", ["hard", "soft"])
@pytest.mark.parametrize("direction", ["a_to_b", "both"])
def test_pair_assigns_trainer_on_same_day_and_shift(mode, direction):
    staff = team()
    staff[0] = force(staff[0])
    settings = config(SameShiftPair("0", "1", mode, direction))
    output = solved(staff, settings)
    assert len(output) == 2
    assert {a.employee_id for a in output} == {"0", "1"}
    assert not same_shift_violations(output, settings)


@pytest.mark.parametrize("direction, expected", [("a_to_b", "optimal"), ("both", "infeasible")])
def test_trainer_may_work_alone_only_in_one_way_mode(direction, expected):
    staff = team()
    staff[0] = replace(staff[0], days_off=frozenset({START}))
    staff[1] = force(staff[1])
    result = solve_schedule(staff, config(SameShiftPair("0", "1", direction=direction)))
    assert result.status == expected


@pytest.mark.parametrize("mode, expected", [("hard", "infeasible"), ("soft", "optimal")])
@pytest.mark.parametrize("conflict", ["holiday", "hours", "allowed", "rest", "consecutive"])
def test_existing_hard_conditions_are_never_relaxed(mode, expected, conflict):
    staff = team(6)
    staff[0] = force(staff[0])
    settings = config(SameShiftPair("0", "1", mode))
    if conflict == "holiday":
        staff[1] = replace(staff[1], days_off=frozenset({START}))
    elif conflict == "hours":
        staff[1] = replace(staff[1], max_monthly_minutes=0)
    elif conflict == "allowed":
        staff[1] = replace(staff[1], allowed_shifts=frozenset({"別勤務"}))
        settings = replace(settings, shift_names=(SHIFT, "別勤務"),
                           shift_times=(ShiftTime(480, 960), ShiftTime(960, 1440)))
    else:
        settings = replace(settings, days=2)
        staff[0] = replace(staff[0], weekly_assignments=(WeeklyAssignment(0, SHIFT), WeeklyAssignment(1, SHIFT)))
        if conflict == "rest":
            staff[1] = replace(staff[1], work_time_overrides=(
                WorkTimeOverride(SHIFT, ShiftTime(480, 1440), START),))
        else:
            settings = replace(settings, max_consecutive_days=1)
            # Aは初日だけ固定、教育担当Bは翌日に固定し、Aの翌日は休み。
            staff[0] = replace(staff[0], weekly_assignments=(WeeklyAssignment(0, SHIFT),),
                               days_off=frozenset({START + timedelta(days=1)}))
            staff[1] = replace(staff[1], weekly_assignments=(WeeklyAssignment(1, SHIFT),))
    result = solve_schedule(staff, settings)
    assert result.status == expected, result.messages
    if mode == "soft":
        assert same_shift_violations(result.assignments, settings)
        assert not audit_schedule(staff, settings, result.assignments)
    else:
        assert any("同一シフトペア" in m for m in result.messages)


@pytest.mark.parametrize("mode, count", [("hard", 3), ("soft", 2)])
def test_only_hard_pair_can_force_extra_staff(mode, count):
    staff = team()
    staff[0], staff[2] = force(staff[0]), force(staff[2])
    settings = config(SameShiftPair("0", "1", mode, weight=100))
    output = solved(staff, settings)
    assert len(output) == count
    assert len(same_shift_violations(output, settings)) == (mode == "soft")


@pytest.mark.parametrize("same_weight, avoid_weight, together", [(10, 1, True), (1, 10, False)])
def test_pair_soft_penalty_is_combined_with_existing_avoidance(same_weight, avoid_weight, together):
    staff = team()
    staff[0] = force(staff[0])
    settings = config(SameShiftPair("0", "1", "soft", weight=same_weight),
                      avoid_pairs=(AvoidPair("0", "1", avoid_weight),))
    output = solved(staff, settings)
    assert (Assignment("1", START, SHIFT) in output) == together


@pytest.mark.parametrize("same_weight, avoid_weight, together", [(10, 1, True), (1, 10, False)])
def test_pair_soft_penalty_is_combined_with_shift_preferences(same_weight, avoid_weight, together):
    staff = team()
    staff[0] = force(staff[0])
    staff[1] = replace(staff[1], avoided_shifts=frozenset({SHIFT}), shift_avoidance_weight=avoid_weight)
    output = solved(staff, config(SameShiftPair("0", "1", "soft", weight=same_weight)))
    assert (Assignment("1", START, SHIFT) in output) == together


def test_both_off_is_satisfied_and_does_not_force_attendance():
    staff = team()
    staff[:2] = [replace(p, monthly_working_minutes=0) for p in staff[:2]]
    settings = config(SameShiftPair("0", "1", direction="both"))
    output = solved(staff, settings)
    assert {a.employee_id for a in output} == {"2", "3"}
    assert not same_shift_violations(output, settings)


def test_hard_pair_is_transitive_across_multiple_trainees():
    staff = team()
    staff[0] = force(staff[0])
    settings = config(same_shift_pairs=(SameShiftPair("0", "1"), SameShiftPair("1", "2")))
    assert {a.employee_id for a in solved(staff, settings)} == {"0", "1", "2"}


def test_same_category_does_not_override_individual_work_times():
    staff = team()
    staff[0] = force(replace(staff[0], work_time_overrides=(WorkTimeOverride(SHIFT, ShiftTime(600, 720)),)))
    settings = config(SameShiftPair("0", "1"))
    output = solved(staff, settings)
    assert len(output) == 3  # 短時間の新人は標準時間帯を終日カバーしない。
    assert not same_shift_violations(output, settings)


@pytest.mark.parametrize("direction, count", [("a_to_b", 1), ("both", 2)])
@pytest.mark.parametrize("mode", ["hard", "soft"])
def test_independent_audit_detects_mismatch_and_soft_counts_each_missing_assignment(direction, count, mode):
    staff = team()
    settings = config(SameShiftPair("0", "1", mode, direction),
                      shift_names=(SHIFT, "夕勤β"), shift_times=(ShiftTime(480, 960), ShiftTime(960, 1440)))
    output = (Assignment("0", START, SHIFT), Assignment("2", START, SHIFT),
              Assignment("1", START, "夕勤β"), Assignment("3", START, "夕勤β"))
    violations = same_shift_violations(output + output[:1], settings)
    assert len(violations) == count  # 重複した入力勤務は二重計上しない。
    errors = audit_schedule(staff, settings, output)
    assert len(errors) == (count if mode == "hard" else 0)
    assert all("同一シフトペア" in error for error in errors)


def test_cross_month_and_custom_night_shift_use_start_date():
    start = date(2026, 1, 31)
    staff = team()
    staff[0] = replace(staff[0], weekly_assignments=(WeeklyAssignment(5, SHIFT), WeeklyAssignment(6, SHIFT)))
    settings = config(SameShiftPair("0", "1"), start_date=start, days=2,
                      shift_times=(ShiftTime(1200, 1920),))
    output = solved(staff, settings)
    assert {Assignment("1", day, SHIFT) for day in settings.dates}.issubset(output)


@pytest.mark.parametrize("pairs, message", [
    (None, "タプル"),
    ((object(),), "SameShiftPair"),
    ((SameShiftPair("0", "0"),), "同じ職員"),
    ((SameShiftPair("0", "unknown"),), "未登録"),
    ((SameShiftPair("0", "1", mode="invalid"),), "hardまたはsoft"),
    ((SameShiftPair("0", "1", direction="invalid"),), "適用範囲"),
    ((SameShiftPair("0", "1", weight=True),), "重み"),
    ((SameShiftPair("0", "1", weight=0),), "重み"),
    ((SameShiftPair("0", "1", weight=101),), "重み"),
    ((SameShiftPair("0", "1"), SameShiftPair("1", "0", "soft")), "逆順"),
    ((SameShiftPair("0", "1"),) * 201, "200組"),
])
def test_invalid_pairs_are_rejected_without_solver_errors(pairs, message):
    settings = config(same_shift_pairs=pairs)
    assert any(message in error for error in validate_inputs(team(), settings))
    assert solve_schedule(team(), settings).status == "invalid"