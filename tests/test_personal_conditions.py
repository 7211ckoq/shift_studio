"""個別勤務時刻・時間帯配置・ソフト回避ペアの独立回帰テスト。"""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, timedelta

import pytest

from optimizer import (
    Assignment, AvoidPair, Employee, Settings, ShiftTime, WorkTimeOverride,
    audit_schedule, capacity_issues, coverage_segments, employee_shift_time,
    pair_conflicts, solve_schedule, validate_inputs,
)


START = date(2026, 1, 1)
SHIFT = "日勤"
BASE = ShiftTime(480, 960)


def config(**changes):
    return replace(Settings(START, days=1, shift_names=(SHIFT,), shift_times=(BASE,),
                            time_limit_seconds=5), **changes)


def team(size=4):
    return [Employee(str(i), f"テスト職員{i}", True) for i in range(size)]


def timed(person, timing, day=None):
    return replace(person, work_time_overrides=(WorkTimeOverride(SHIFT, timing, day),))


def assignments(staff, day=START, shift=SHIFT):
    return tuple(Assignment(person.employee_id, day, shift) for person in staff)


def solved(staff, settings):
    result = solve_schedule(staff, settings)
    assert result.status == "optimal", result.messages
    assert audit_schedule(staff, settings, result.assignments) == []
    return result


def test_exact_api_defaults_positional_compatibility_and_frozen_dataclasses():
    assert [f.name for f in fields(WorkTimeOverride)] == ["shift", "timing", "day"]
    assert [f.name for f in fields(AvoidPair)] == ["employee_id_a", "employee_id_b", "weight"]
    assert fields(Employee)[-1].name == "work_time_overrides"
    assert fields(Settings)[-1].name == "avoid_pairs"
    assert Employee("x", "名前", True, frozenset(), None, None, None).work_time_overrides == ()
    assert Settings(START).avoid_pairs == ()
    assert WorkTimeOverride(SHIFT, BASE).day is None
    assert AvoidPair("0", "1").weight == 1
    for obj, attr, value in [(WorkTimeOverride(SHIFT, BASE), "day", START),
                             (AvoidPair("0", "1"), "weight", 2)]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, value)


@pytest.mark.parametrize("dated_first", [False, True])
def test_dated_override_precedes_recurring_independent_of_order(dated_first):
    recurring, dated = ShiftTime(420, 960, 30), ShiftTime(480, 1020, 60)
    overrides = (WorkTimeOverride(SHIFT, recurring), WorkTimeOverride(SHIFT, dated, START))
    person = replace(team(1)[0], work_time_overrides=overrides[::-1] if dated_first else overrides)
    settings = config(days=2)
    assert employee_shift_time(person, settings, START, SHIFT) == dated
    assert employee_shift_time(person, settings, START + timedelta(days=1), SHIFT) == recurring
    assert employee_shift_time(team(1)[0], settings, START, SHIFT) == BASE


def test_override_only_applies_to_its_category_and_does_not_force_assignment():
    settings = config(shift_names=(SHIFT, "別区分"), shift_times=(BASE, BASE))
    staff = team(5)
    staff[0] = replace(timed(staff[0], ShiftTime(420, 1020), START), monthly_working_minutes=0)
    assert employee_shift_time(staff[0], settings, START, "別区分") == BASE
    result = solved(staff, settings)
    assert not any(a.employee_id == "0" for a in result.assignments)
    assert len(result.assignments) == 4


def test_coverage_segments_split_all_effective_boundaries_and_keep_input_id_order():
    staff = team(5)
    staff[1] = timed(staff[1], ShiftTime(420, 720))
    staff[2] = timed(staff[2], ShiftTime(720, 1020))
    staff[3] = timed(staff[3], ShiftTime(600, 840, 60))
    staff[4] = timed(staff[4], ShiftTime(0, 480))
    assert coverage_segments(staff, config(), START, SHIFT) == (
        (480, 600, ("0", "1")), (600, 720, ("0", "1", "3")),
        (720, 840, ("0", "2", "3")), (840, 960, ("0", "2")),
    )


def test_coverage_eligibility_filters_allowed_shift_off_and_effective_carryover():
    settings = config(shift_names=(SHIFT, "別区分"), shift_times=(BASE, BASE))
    staff = team(5)
    staff[1] = replace(staff[1], allowed_shifts=frozenset({"別区分"}))
    staff[2] = replace(staff[2], days_off=frozenset({START}))
    staff[3] = replace(timed(staff[3], ShiftTime(480, 1441)),
                       days_off=frozenset({START + timedelta(days=1)}))
    staff[4] = replace(timed(staff[4], ShiftTime(480, 1440)),
                       days_off=frozenset({START + timedelta(days=1)}))
    assert coverage_segments(staff, settings, START, SHIFT) == ((480, 960, ("0", "4")),)


@pytest.mark.parametrize("timing", [ShiftTime(420, 900), ShiftTime(540, 1020), ShiftTime(600, 840)])
def test_early_late_and_short_staff_cannot_leave_uncovered_edges(timing):
    staff = team(2)
    staff[0] = timed(staff[0], timing)
    result = solve_schedule(staff, config())
    assert result.status == "infeasible"
    assert any("個別勤務時刻" in msg for msg in result.messages)
    assert any("時間帯の最低人数" in msg for msg in audit_schedule(staff, config(), assignments(staff)))


def test_complementary_partial_workers_require_four_assignments():
    staff = [timed(p, ShiftTime(480, 720) if i < 2 else ShiftTime(720, 960))
             for i, p in enumerate(team())]
    result = solved(staff, config())
    assert len(result.assignments) == 4
    assert coverage_segments(staff, config(), START, SHIFT) == (
        (480, 720, ("0", "1")), (720, 960, ("2", "3")),
    )


def test_audit_uses_assigned_workers_not_all_eligible_workers():
    staff = team(3)
    staff[1] = timed(staff[1], ShiftTime(480, 720))
    errors = audit_schedule(staff, config(), assignments(staff[:2]))
    assert any("時間帯の最低人数" in msg for msg in errors)
    assert not any("時間帯" not in msg and "最低人数" in msg for msg in errors)


def test_each_shift_requires_leader_on_every_segment_not_just_any_assignment():
    staff = [replace(p, is_leader=i == 0) for i, p in enumerate(team(3))]
    staff[0] = timed(staff[0], ShiftTime(480, 720))
    assert solve_schedule(staff, config()).status == "infeasible"
    errors = audit_schedule(staff, config(), assignments(staff))
    assert any("時間帯に主任以上が不在" in msg for msg in errors)
    assert not any(msg.endswith(": 主任以上が不在です。") for msg in errors)


def test_each_day_leader_may_be_partial_but_must_be_assigned():
    staff = [replace(p, is_leader=i == 0) for i, p in enumerate(team(3))]
    staff[0] = timed(staff[0], ShiftTime(480, 720))
    settings = config(leader_mode="each_day")
    result = solved(staff, settings)
    assert len(result.assignments) == 3
    assert any("主任以上が不在" in msg for msg in audit_schedule(staff, settings, assignments(staff[1:])))


def test_leader_coverage_can_be_shared_by_complementary_partial_leaders():
    staff = [replace(p, is_leader=i < 2) for i, p in enumerate(team())]
    staff[0] = timed(staff[0], ShiftTime(480, 720))
    staff[1] = timed(staff[1], ShiftTime(720, 960))
    result = solved(staff, config())
    assert len(result.assignments) == 3
    assert {"0", "1"}.issubset({a.employee_id for a in result.assignments})


def test_temporal_coverage_cannot_be_borrowed_from_another_category():
    settings = config(shift_names=(SHIFT, "別区分"), shift_times=(BASE, BASE))
    staff = team(4)
    staff = [replace(p, allowed_shifts=frozenset({SHIFT if i < 2 else "別区分"}))
             for i, p in enumerate(staff)]
    staff[0] = timed(staff[0], ShiftTime(480, 720))
    assert solve_schedule(staff, settings).status == "infeasible"
    output = assignments(staff[:2]) + assignments(staff[2:], shift="別区分")
    assert any(SHIFT in msg and "時間帯の最低人数" in msg for msg in audit_schedule(staff, settings, output))


def test_coverage_is_per_start_day_not_previous_days_carryover():
    settings = config(days=2, shift_times=(ShiftTime(1200, 1500),), minimum_rest_hours=0)
    staff = team(4)
    staff[0] = timed(staff[0], ShiftTime(1380, 1560), START + timedelta(days=1))
    output = assignments(staff[2:]) + assignments(staff[:2], START + timedelta(days=1))
    errors = audit_schedule(staff, settings, output)
    assert any(str(START + timedelta(days=1)) in msg and "時間帯の最低人数" in msg for msg in errors)


def test_custom_end_at_midnight_avoids_false_base_carryover_rejection():
    settings = config(shift_times=(ShiftTime(1200, 1500),), leader_mode="each_day")
    staff = team(3)
    staff[0] = replace(timed(staff[0], ShiftTime(1200, 1440)),
                       days_off=frozenset({START + timedelta(days=1)}), is_leader=True)
    staff[1:] = [replace(p, is_leader=False) for p in staff[1:]]
    assert not capacity_issues(staff, settings)
    result = solved(staff, settings)
    assert len(result.assignments) == 3
    assert "休み希望日に勤務があります。" not in audit_schedule(staff, settings, result.assignments)


@pytest.mark.parametrize("end,violates", [(1440, False), (1441, True), (1920, True)])
def test_effective_carryover_uses_calendar_off_including_after_period(end, violates):
    staff = team(3)
    staff[0] = replace(timed(staff[0], ShiftTime(480, end)),
                       days_off=frozenset({START + timedelta(days=1)}))
    errors = audit_schedule(staff, config(), assignments(staff[:2]))
    assert ("休み希望日に勤務があります。" in errors) is violates
    ids = {a.employee_id for a in solved(staff, config()).assignments}
    if violates:
        assert "0" not in ids


def test_dated_override_can_remove_recurring_carryover_on_requested_off_eve():
    person = replace(team(1)[0], days_off=frozenset({START + timedelta(days=1)}),
                     work_time_overrides=(WorkTimeOverride(SHIFT, ShiftTime(480, 1500)),
                                          WorkTimeOverride(SHIFT, BASE, START)))
    staff = [person, team(2)[1]]
    solved(staff, config())


@pytest.mark.parametrize("end,feasible", [(1740, True), (1741, False)])
def test_dated_effective_rest_minute_boundary_and_break_not_subtracted(end, feasible):
    # 翌日08:00まで3時間を確保できる終了境界は当日29:00。
    staff = team(2)
    # 開始05:01、最大24時間。休憩を引いて休息を長く見せてはいけない。
    staff[0] = timed(staff[0], ShiftTime(301, end, 300), START)
    settings = config(days=2, minimum_rest_hours=3)
    # 08:00翌日までの休息は180分/179分。
    result = solve_schedule(staff, settings)
    assert result.has_solution is feasible
    output = assignments(staff) + assignments(staff, START + timedelta(days=1))
    errors = audit_schedule(staff, settings, output)
    assert any("休息が不足" in msg for msg in errors) is (not feasible)


@pytest.mark.parametrize("end,feasible", [(2700, True), (2701, False)])
def test_effective_gap_two_rest_is_checked_in_solver_and_audit(end, feasible):
    middle, last = START + timedelta(days=1), START + timedelta(days=2)
    staff = [replace(p, is_leader=i < 2) for i, p in enumerate(team())]
    staff[0] = timed(staff[0], ShiftTime(1380, end, 100), START)
    # Aは初日と最終日の唯一の主任。中日はBが主任を務める。
    staff[1] = replace(staff[1], days_off=frozenset({START, last}))
    settings = config(days=3, leader_mode="each_day", minimum_rest_hours=11)
    # 必須主任Aの初日終了→最終日開始の休息は660分/659分。
    # 中日の勤務は初日の持越しと重なるため選択できず、gap=2の検査が必要。
    result = solve_schedule(staff, settings)
    assert result.has_solution is feasible, result.messages
    output = (assignments(staff[2:]) + (Assignment("0", START, SHIFT),)
              + assignments(staff[1:], middle)
              + assignments([staff[0], *staff[2:]], last))
    errors = audit_schedule(staff, settings, output)
    assert any("休息が不足" in msg for msg in errors) is (not feasible)


@pytest.mark.parametrize("break_minutes,feasible", [(60, True), (59, False), (61, False)])
def test_effective_break_minutes_used_for_exact_monthly_work(break_minutes, feasible):
    staff = [replace(timed(p, ShiftTime(420, 960, break_minutes)), monthly_working_minutes=31 * 480)
             for p in team(2)]
    settings = config()
    result = solve_schedule(staff, settings)
    assert result.has_solution is feasible
    errors = audit_schedule(staff, settings, assignments(staff))
    assert any("指定時間と一致" in msg for msg in errors) is (not feasible)


@pytest.mark.parametrize("cap,feasible", [(540, True), (539, False)])
def test_effective_early_start_counts_extra_minutes_towards_monthly_cap(cap, feasible):
    staff = [replace(timed(p, ShiftTime(420, 960)), max_monthly_minutes=cap) for p in team(2)]
    result = solve_schedule(staff, config())
    assert result.has_solution is feasible
    errors = audit_schedule(staff, config(), assignments(staff))
    assert any("上限を超え" in msg for msg in errors) is (not feasible)


def test_short_personal_working_time_can_meet_exact_time_with_complementary_coverage():
    staff = [replace(timed(p, ShiftTime(480, 720) if i < 2 else ShiftTime(720, 960)),
                     monthly_working_minutes=31 * 240) for i, p in enumerate(team())]
    assert len(solved(staff, config()).assignments) == 4


def test_dated_month_crossing_effective_times_count_in_each_start_month():
    jan, feb = date(2026, 1, 31), date(2026, 2, 1)
    settings = config(start_date=jan, days=2, shift_times=(ShiftTime(1200, 1500),), minimum_rest_hours=0)
    staff = [replace(p, monthly_working_minutes=26040,
                     work_time_overrides=(WorkTimeOverride(SHIFT, ShiftTime(1200, 2160, 120), jan),
                                          WorkTimeOverride(SHIFT, ShiftTime(1200, 2280, 150), feb)))
             for p in team(2)]
    # 月26040分の日割りは1月840分、2月930分。翌月への分割計上はしない。
    result = solved(staff, settings)
    assert len(result.assignments) == 4
    capped = [replace(p, monthly_working_minutes=None, max_monthly_minutes=930) for p in staff]
    solved(capped, settings)
    capped = [replace(p, max_monthly_minutes=929) for p in capped]
    assert solve_schedule(capped, settings).status == "infeasible"
    assert any("2026-02" in msg and "上限を超え" in msg
               for msg in audit_schedule(capped, settings, result.assignments))


def test_year_crossing_dated_override_and_next_year_requested_off():
    day, next_day = date(2026, 12, 31), date(2027, 1, 1)
    settings = config(start_date=day)
    staff = team(3)
    staff[0] = replace(timed(staff[0], ShiftTime(480, 1441), day), days_off=frozenset({next_day}))
    result = solved(staff, settings)
    assert all(a.employee_id != "0" for a in result.assignments)


def test_soft_pair_is_avoided_when_possible():
    settings = config(avoid_pairs=(AvoidPair("0", "1"),))
    result = solved(team(3), settings)
    assert len(result.assignments) == 2
    assert pair_conflicts(result.assignments, settings) == ()


def test_unavoidable_pair_is_soft_and_never_audit_violation():
    pair = AvoidPair("0", "1", 100)
    settings = config(avoid_pairs=(pair,))
    result = solved(team(2), settings)
    assert pair_conflicts(result.assignments, settings) == ((pair, START, SHIFT),)


def test_pair_weights_choose_lowest_total_conflict_cost():
    pairs = (AvoidPair("0", "1", 100), AvoidPair("0", "2", 2), AvoidPair("1", "2", 1))
    settings = config(avoid_pairs=pairs)
    result = solved(team(3), settings)
    assert {a.employee_id for a in result.assignments} == {"1", "2"}
    assert pair_conflicts(result.assignments, settings) == ((pairs[2], START, SHIFT),)


def test_pair_weighted_cost_is_sum_not_number_of_conflicts():
    staff = [replace(p, is_leader=i == 0) for i, p in enumerate(team())]
    pairs = (AvoidPair("0", "1", 5), AvoidPair("0", "2", 2), AvoidPair("0", "3", 2),
             AvoidPair("2", "3", 1))
    settings = config(minimum_staff=3, avoid_pairs=pairs)
    result = solved(staff, settings)
    assert {a.employee_id for a in result.assignments} == {"0", "2", "3"}
    assert sum(p.weight for p, _, _ in pair_conflicts(result.assignments, settings)) == 5


def test_placement_count_has_priority_over_even_max_weight_pair():
    staff = team()
    staff[2] = timed(staff[2], ShiftTime(480, 720))
    staff[3] = timed(staff[3], ShiftTime(720, 960))
    settings = config(avoid_pairs=(AvoidPair("0", "1", 100),))
    # 0+2+3ならペア衝突なしだが、0+1の2配置が必ず優先。
    result = solved(staff, settings)
    assert {a.employee_id for a in result.assignments} == {"0", "1"}
    assert len(pair_conflicts(result.assignments, settings)) == 1


def test_pair_avoidance_has_priority_over_fairness():
    staff = [replace(p, is_leader=i == 0) for i, p in enumerate(team(3))]
    settings = config(days=2, avoid_pairs=(AvoidPair("0", "1"),))
    result = solved(staff, settings)
    assert [a.employee_id for a in result.assignments].count("2") == 2
    assert not pair_conflicts(result.assignments, settings)


def test_pair_conflicts_count_same_category_not_temporal_overlap_or_duplicate_rows():
    pair = AvoidPair("0", "1")
    settings = config(days=2, shift_names=(SHIFT, "別区分"), shift_times=(BASE, BASE), avoid_pairs=(pair,))
    output = (Assignment("0", START, SHIFT), Assignment("0", START, SHIFT),
              Assignment("1", START, SHIFT), Assignment("1", START, "別区分"),
              Assignment("0", START + timedelta(days=1), "別区分"))
    assert pair_conflicts(output, settings) == ((pair, START, SHIFT),)
    assert pair_conflicts((), settings) == ()
    staff = [replace(p, allowed_shifts=frozenset({SHIFT if i < 2 else "別区分"}))
             for i, p in enumerate(team())]
    settings = replace(settings, avoid_pairs=(AvoidPair("0", "2"),))
    assert not pair_conflicts(solved(staff, settings).assignments, settings)


def test_pair_conflicts_include_disjoint_personal_times_in_same_category():
    pair = AvoidPair("0", "1")
    staff = team(2)
    staff[0] = timed(staff[0], ShiftTime(480, 720))
    staff[1] = timed(staff[1], ShiftTime(720, 960))
    settings = config(avoid_pairs=(pair,))
    assert pair_conflicts(assignments(staff), settings) == ((pair, START, SHIFT),)


@pytest.mark.parametrize("timing", [
    None, "08:00", (480, 960), ShiftTime(-1, 960), ShiftTime(1440, 1500),
    ShiftTime(480, 480), ShiftTime(481, 480), ShiftTime(0, 1441),
    ShiftTime(True, 960), ShiftTime(480.0, 960), ShiftTime(480, 960.0),
    ShiftTime(480, 960, -1), ShiftTime(480, 960, 480), ShiftTime(480, 960, 481),
    ShiftTime(480, 960, True), ShiftTime(480, 960, 0.5),
])
def test_invalid_override_timing_types_ranges_and_breaks(timing):
    staff = team()
    staff[0] = timed(staff[0], timing)
    assert solve_schedule(staff, config()).status == "invalid"


@pytest.mark.parametrize("overrides", [
    (None,), ("不正",), (WorkTimeOverride("不存在", BASE),),
    (WorkTimeOverride(SHIFT, BASE, "2026-01-01"),),
    (WorkTimeOverride(SHIFT, BASE, START - timedelta(days=1)),),
    (WorkTimeOverride(SHIFT, BASE, START + timedelta(days=1)),),
    (WorkTimeOverride(SHIFT, BASE), WorkTimeOverride(SHIFT, ShiftTime(420, 960))),
    (WorkTimeOverride(SHIFT, BASE, START), WorkTimeOverride(SHIFT, BASE, START)),
])
def test_invalid_override_shift_date_duplicate_or_object(overrides):
    staff = team()
    staff[0] = replace(staff[0], work_time_overrides=overrides)
    assert solve_schedule(staff, config()).status == "invalid"


def test_override_on_forbidden_shift_is_rejected():
    staff = team()
    staff[0] = replace(timed(staff[0], BASE), allowed_shifts=frozenset({"別区分"}))
    settings = config(shift_names=(SHIFT, "別区分"), shift_times=(BASE, BASE))
    assert solve_schedule(staff, settings).status == "invalid"


@pytest.mark.parametrize("pairs", [
    (None,), ("不正",), (AvoidPair("0", "0"),), (AvoidPair("0", "不存在"),),
    (AvoidPair("0", "1"), AvoidPair("0", "1", 2)),
    (AvoidPair("0", "1"), AvoidPair("1", "0")),
    (AvoidPair("0", "1", 0),), (AvoidPair("0", "1", 101),),
    (AvoidPair("0", "1", True),), (AvoidPair("0", "1", 1.5),),
])
def test_invalid_pair_definitions(pairs):
    assert solve_schedule(team(), config(avoid_pairs=pairs)).status == "invalid"


def test_pair_count_safety_limit_accepts_200_unique_pairs_rejects_201():
    staff = team(21)
    pairs = tuple(AvoidPair(a.employee_id, b.employee_id) for i, a in enumerate(staff) for b in staff[i + 1:])
    assert not validate_inputs(staff, config(avoid_pairs=pairs[:200]))
    assert any("200組" in msg for msg in validate_inputs(staff, config(avoid_pairs=pairs[:201])))


@pytest.mark.parametrize("changes", [{"start_date": "不正"}, {"days": 0}, {"days": 63}])
def test_bad_settings_do_not_crash_override_date_validation(changes):
    staff = [timed(p, BASE, START) for p in team(2)]
    assert solve_schedule(staff, config(**changes)).status == "invalid"


def test_date_range_overflow_is_reported_not_raised():
    assert solve_schedule(team(), config(start_date=date.max, days=2)).status == "invalid"


def test_default_configuration_still_has_original_three_categories_and_minimum_placement():
    settings = Settings(START, days=2, time_limit_seconds=5)
    staff = team(9)
    result = solved(staff, settings)
    assert len(result.assignments) == 12
    assert pair_conflicts(result.assignments, settings) == ()
    assert coverage_segments(staff, settings, START, "日勤") == ((480, 960, tuple(p.employee_id for p in staff)),)


def test_general_infeasible_message_mentions_personalized_coverage():
    # 事前容量検査では矛盾しないが、個人の0分指定によりモデルは不可能。
    staff = [replace(p, monthly_working_minutes=0) for p in team(2)]
    result = solve_schedule(staff, config())
    assert result.status == "infeasible"
    assert any("個別勤務時刻" in msg for msg in result.messages)