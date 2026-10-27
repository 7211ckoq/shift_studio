from dataclasses import replace
from datetime import date

import pytest

from optimizer import (
    Assignment,
    Employee,
    RoleRequirement,
    Settings,
    ShiftTime,
    WorkTimeOverride,
    audit_schedule,
    capacity_issues,
    employee_has_role,
    solve_schedule,
    validate_inputs,
)


START = date(2026, 11, 1)


def settings(**changes):
    return replace(
        Settings(
            START,
            days=1,
            minimum_staff=2,
            minimum_rest_hours=0,
            shift_names=("日勤", "夜勤"),
            shift_times=(ShiftTime(480, 960), ShiftTime(960, 1440)),
            time_limit_seconds=5,
        ),
        **changes,
    )


def staff_with_roles():
    return [
        Employee("A", "職員A", roles=frozenset({"看護"})),
        Employee("B", "職員B", roles=frozenset({"看護"})),
        Employee("C", "職員C", roles=frozenset({"看護"})),
        Employee("D", "職員D", roles=frozenset({"看護"})),
    ]


def test_employee_has_role_preserves_legacy_and_explicit_membership():
    assert employee_has_role(Employee("A", "旧主任", is_leader=True), "主任")
    assert not employee_has_role(Employee("A", "旧主任", is_leader=True), "看護")
    assert not employee_has_role(
        Employee("B", "明示なし", is_leader=True, roles=frozenset()), "主任"
    )
    assert employee_has_role(
        Employee("C", "明示あり", is_leader=False, roles=frozenset({"看護"})), "看護"
    )


def test_role_none_mode_disables_legacy_leader_requirement():
    employees = [replace(person, is_leader=False) for person in staff_with_roles()]
    result = solve_schedule(
        employees,
        settings(role_requirements=(RoleRequirement("看護", "none"),)),
    )
    assert result.has_solution, result.messages
    assert not audit_schedule(employees, settings(role_requirements=(RoleRequirement("看護"),)), result.assignments)


def test_each_shift_role_minimum_can_exceed_generic_staff_minimum():
    employees = staff_with_roles()
    config = settings(role_requirements=(RoleRequirement("看護", "each_shift", 2),))
    result = solve_schedule(employees, config)
    assert result.has_solution, result.messages
    role_members = {person.employee_id for person in employees if employee_has_role(person, "看護")}
    for shift in config.shift_names:
        members = {
            assignment.employee_id
            for assignment in result.assignments
            if assignment.shift == shift
        }
        assert len(members & role_members) >= 2
    assert audit_schedule(employees, config, result.assignments) == []


def test_each_day_role_minimum_counts_across_all_shifts():
    employees = staff_with_roles()
    config = settings(
        leader_mode="invalid",  # role configuration fully supersedes the legacy mode
        role_requirements=(RoleRequirement("看護", "each_day", 2),),
    )
    result = solve_schedule(employees, config)
    assert result.has_solution, result.messages
    assert len({a.employee_id for a in result.assignments if a.employee_id in {"A", "B"}}) >= 2
    assert audit_schedule(employees, config, result.assignments) == []


def test_explicit_roles_ignore_is_leader_flag_for_configured_role():
    employees = [
        Employee("A", "主任フラグのみA", is_leader=True, roles=frozenset()),
        Employee("B", "主任フラグのみB", is_leader=True, roles=frozenset()),
    ]
    config = settings(
        shift_names=("日勤",),
        shift_times=(ShiftTime(480, 960),),
        role_requirements=(RoleRequirement("主任", "each_shift"),),
    )
    result = solve_schedule(employees, config)
    assert result.status == "infeasible"
    assert any("主任" in message for message in result.messages)


def test_legacy_people_can_supply_chief_role_in_mixed_role_data():
    employees = [
        Employee("A", "旧形式主任", is_leader=True),
        Employee("B", "明示的に役割なし", is_leader=True, roles=frozenset()),
    ]
    config = settings(
        shift_names=("日勤",),
        shift_times=(ShiftTime(480, 960),),
        role_requirements=(RoleRequirement("主任", "each_shift"),),
    )
    result = solve_schedule(employees, config)
    assert result.has_solution, result.messages
    assert any(a.employee_id == "A" for a in result.assignments)
    assert audit_schedule(employees, config, result.assignments) == []


def test_each_shift_role_is_required_in_every_coverage_segment():
    employees = [
        Employee(
            "A", "前半の看護", roles=frozenset({"看護"}),
            work_time_overrides=(),
        ),
        Employee("B", "後半", roles=frozenset()),
    ]
    config = settings(
        shift_names=("日勤",),
        shift_times=(ShiftTime(480, 960),),
        role_requirements=(RoleRequirement("看護", "each_shift"),),
    )
    employees[0] = replace(
        employees[0],
        work_time_overrides=(
            # only part of the assigned shift is covered by this role member
            WorkTimeOverride("日勤", ShiftTime(480, 720)),
        ),
    )
    assignments = (Assignment("A", START, "日勤"),)
    errors = audit_schedule(employees, config, assignments)
    assert any("看護" in message and "720–960分" in message for message in errors)
    assert any("看護" in message and "720–960分" in message for message in capacity_issues(employees, config))
    assert solve_schedule(employees, config).status == "infeasible"


def test_each_day_audit_requires_the_configured_role_count():
    employees = staff_with_roles()
    config = settings(role_requirements=(RoleRequirement("看護", "each_day", 2),))
    errors = audit_schedule(employees, config, (Assignment("A", START, "日勤"),))
    assert any("看護" in message and "1日配置人数" in message for message in errors)


@pytest.mark.parametrize(
    "requirements",
    [
        (RoleRequirement(""),),
        (RoleRequirement(" 看護"),),
        (RoleRequirement("休"),),
        (RoleRequirement("看護"), RoleRequirement("看護")),
        (RoleRequirement("看護", "unknown"),),
        (RoleRequirement("看護", minimum=0),),
        (RoleRequirement("看護", minimum=True),),
        (RoleRequirement("看護", minimum=5),),
    ],
)
def test_invalid_role_requirements_are_rejected(requirements):
    config = settings(role_requirements=requirements)
    assert validate_inputs(staff_with_roles(), config)
    assert solve_schedule(staff_with_roles(), config).status == "invalid"


def test_employee_roles_must_be_string_sets_and_known_when_configured():
    config = settings(role_requirements=(RoleRequirement("看護"),))
    for value in ("看護", ["看護"], {1}, {"未定義"}):
        employees = staff_with_roles()
        employees[0] = replace(employees[0], roles=value)
        assert validate_inputs(employees, config)
        assert solve_schedule(employees, config).status == "invalid"


def test_legacy_leader_rules_are_unchanged_without_role_requirements():
    employees = [Employee("A", "A", is_leader=True), Employee("B", "B")]
    config = settings(shift_names=("日勤",), shift_times=(ShiftTime(480, 960),))
    assert solve_schedule(employees, config).has_solution
    no_leaders = [replace(person, is_leader=False) for person in employees]
    assert solve_schedule(no_leaders, config).status == "infeasible"
