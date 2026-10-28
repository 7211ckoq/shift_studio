"""手修正したマスを固定し、残りを最小の変更で作り直す機能のテスト。"""

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays
from optimizer import Employee, Settings, audit_schedule, pin_errors, solve_schedule


START = date(2026, 11, 2)


def team(size=8):
    return [Employee(f"S{i:02}", f"職員{i}", True) for i in range(size)]


def config(**changes):
    return replace(Settings(START, days=5, minimum_staff=1, minimum_rest_hours=0, time_limit_seconds=10), **changes)


def as_map(assignments):
    return {(a.employee_id, a.day): a.shift for a in assignments}


def test_pins_are_kept_and_other_changes_are_minimal():
    employees, settings = team(), config()
    first = solve_schedule(employees, settings)
    assert first.has_solution
    before = as_map(first.assignments)
    worker, day = next(iter(before))
    idle = next(p.employee_id for p in employees if (p.employee_id, day) not in before)
    # 勤務者を休みに、休みの人をその勤務に固定する（入れ替え）。
    fixed = {(worker, day): None, (idle, day): before[worker, day]}
    revised = solve_schedule(employees, settings, fixed=fixed, previous=first.assignments)
    assert revised.has_solution, revised.messages
    after = as_map(revised.assignments)
    assert (worker, day) not in after
    assert after[idle, day] == before[worker, day]
    assert audit_schedule(employees, settings, revised.assignments) == []
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)} - set(fixed)
    assert changed == set()


def test_without_previous_pins_still_apply():
    employees, settings = team(), config(days=2)
    result = solve_schedule(employees, settings, fixed={("S07", START): "準夜", ("S00", START): None})
    assert result.has_solution
    mapping = as_map(result.assignments)
    assert mapping["S07", START] == "準夜" and ("S00", START) not in mapping


def test_conflicting_pins_are_infeasible_not_relaxed():
    employees, settings = team(3), config(days=1)
    fixed = {("S00", START): None, ("S01", START): None}  # 3シフトに1人ずつ必要なのに2人休み
    result = solve_schedule(employees, settings, fixed=fixed)
    assert result.status == "infeasible"
    assert any("固定" in message for message in result.messages)


@pytest.mark.parametrize("value, setting_changes, person_changes, message", [
    ("日勤", {"closed_days": frozenset({START})}, {}, "休日"),
    ("日勤", {}, {"days_off": frozenset({START})}, "希望休"),
    ("日勤", {}, {"allowed_shifts": frozenset({"深夜"})}, "許可シフト"),
    ("早番", {}, {}, "シフト一覧"),
])
def test_invalid_pins(value, setting_changes, person_changes, message):
    employees = team()
    employees[0] = replace(employees[0], **person_changes)
    settings = config(**setting_changes)
    errors = pin_errors(employees, settings, {("S00", START): value})
    assert any(message in error for error in errors), errors
    assert solve_schedule(employees, settings, fixed={("S00", START): value}).status == "invalid"


def ui_app():
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()
    at.date_input[0].set_value((START, START + timedelta(days=2))).run()
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(10).run()
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    assert at.session_state.schedule[1].has_solution
    return at



def edit_cell(at, pid, day, value, editor="manual_editor"):
    """手修正用の表（editor="manual_locks" なら固定チェック表）を、画面で書き換えた場合と同じ編集状態にする。"""
    key = f"{editor}_{at.session_state.manual_revision}"
    result = at.session_state.schedule[1]
    employees = at.session_state.schedule[2]
    row = next(i for i, p in enumerate(employees) if p.employee_id == pid)
    column = day if isinstance(day, str) else f"{day:%m/%d}({'月火水木金土日'[day.weekday()]})"
    state = at.session_state[key] if key in at.session_state else {"edited_rows": {}, "added_rows": [], "deleted_rows": []}
    state = {**state, "edited_rows": {**state.get("edited_rows", {}), row: {**state.get("edited_rows", {}).get(row, {}), column: value}}}
    at.session_state[key] = state
    at.run()
    assert not at.exception
    return result


def button(at, label):
    return next(b for b in at.button if b.label == label)


def test_ui_manual_edit_check_resolve_and_clear():
    at = ui_app()
    first = at.session_state.schedule[1]
    before = as_map(first.assignments)
    worker, day = next((key for key in before if key[1] == START))
    idle = next(p.employee_id for p in at.session_state.schedule[2] if (p.employee_id, START) not in before)
    shift = before[worker, day]
    edit_cell(at, idle, START, shift)
    pins_table = next(f.value for f in at.dataframe if "固定内容" in f.value.columns)
    assert pins_table.to_dict("records") == [
        {"職員ID": idle, "氏名": pins_table.iloc[0]["氏名"], "固定日": START.isoformat(), "固定内容": shift, "状態": "今回の手修正"}]
    # 1人増やしただけなら必須条件は満たす（最大人数なし）。
    button(at, "手修正のまま検査").click().run()
    assert any("満たしています" in s.value for s in at.success)
    edit_cell(at, worker, START, "休")
    button(at, "固定して残りを作り直す").click().run()
    assert not at.exception
    revised = at.session_state.schedule[1]
    after = as_map(revised.assignments)
    assert after[idle, START] == shift and (worker, START) not in after
    assert at.session_state.manual_pins == {(idle, START): shift, (worker, START): None}
    assert any("固定した2マスを守って作り直しました" in s.value for s in at.success)
    assert any("固定したマスです（2マス）" in c.value for c in at.caption)
    button(at, "固定をすべて解除").click().run()
    assert at.session_state.manual_pins == {}


def test_ui_manual_check_reports_violations_and_new_generation_clears_pins():
    at = ui_app()
    before = as_map(at.session_state.schedule[1].assignments)
    worker = next(key for key in before if key[1] == START)[0]
    edit_cell(at, worker, START, "休")
    button(at, "手修正のまま検査").click().run()
    assert any("必須条件を満たしません" in e.value for e in at.error)
    at.session_state.manual_pins = {(worker, START): None}
    button(at, "シフトを作成").click().run()
    assert at.session_state.manual_pins == {}


def test_ui_lock_checkbox_pins_current_value_and_unchecking_releases():
    at = ui_app()
    before = as_map(at.session_state.schedule[1].assignments)
    employees = at.session_state.schedule[2]
    idle = next(p.employee_id for p in employees if (p.employee_id, START) not in before)
    worker, day = next(key for key in before if key[1] == START + timedelta(days=1) and key[0] != idle)
    edit_cell(at, worker, day, True, editor="manual_locks")
    edit_cell(at, idle, "全日固定", True, editor="manual_locks")
    pins_table = next(f.value for f in at.dataframe if "固定内容" in f.value.columns)
    assert set(pins_table["状態"]) == {"今回チェック"}
    assert len(pins_table) == 1 + 3
    button(at, "固定して残りを作り直す").click().run()
    assert not at.exception
    pins = at.session_state.manual_pins
    assert pins[worker, day] == before[worker, day]
    assert all(pins[idle, START + timedelta(days=i)] == before.get((idle, START + timedelta(days=i))) for i in range(3))
    assert any("固定した4マスを守って作り直しました。固定以外で変わったマスは0マス" in s.value for s in at.success)
    # 新しい表では固定済みのマスにチェックが付き、外すと固定を解除できる。
    locks = next(f.value for f in at.dataframe if "全日固定" in f.value.columns)
    column = f"{day:%m/%d}({'月火水木金土日'[day.weekday()]})"
    assert locks.set_index("職員ID").loc[worker, column]
    edit_cell(at, worker, day, False, editor="manual_locks")
    button(at, "固定して残りを作り直す").click().run()
    assert (worker, day) not in at.session_state.manual_pins
    assert len(at.session_state.manual_pins) == 3


def test_ui_edited_cell_is_pinned_even_without_check():
    at = ui_app()
    before = as_map(at.session_state.schedule[1].assignments)
    employees = at.session_state.schedule[2]
    idle = next(p.employee_id for p in employees if (p.employee_id, START) not in before)
    shift = at.session_state.schedule[3].shift_names[0]
    edit_cell(at, idle, START, shift)
    edit_cell(at, idle, START, False, editor="manual_locks")
    pins_table = next(f.value for f in at.dataframe if "固定内容" in f.value.columns)
    assert pins_table.to_dict("records")[0]["状態"] == "今回の手修正"
