"""シフト統合とワンオペを実画面に通すテスト。"""

from datetime import timedelta
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays

from test_personal_app import START, generate, table


def three_shift_app():
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()
    assert not at.exception
    at.date_input[0].set_value((START, START + timedelta(days=2 - 1)))
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(5)
    next(n for n in at.number_input if n.label == "各シフトの最低人数 N").set_value(1)
    at.run()
    assert not at.exception
    return at


def test_ui_merges_selected_day_and_allows_one_person_operation():
    at = three_shift_app()
    at.session_state.merge_source = pd.DataFrame([
        {"日付": START.isoformat(), "統合するシフト": ["日勤", "準夜"], "休憩（分）": 60},
    ])
    at.session_state.merge_revision += 1
    at.run()
    assert not at.exception
    generate(at)
    result = at.session_state.schedule[1]
    assert result.has_solution, result.messages
    # 初日は深夜＋（日勤+準夜）の2勤務、2日目は3勤務。いずれも1人ずつ。
    assert len(result.assignments) == 2 + 3
    daily = table(at, "日付", "シフト", "人数")
    first = daily[daily["日付"] == START.isoformat()]
    assert list(first["シフト"]) == ["深夜", "日勤+準夜"]
    assert set(first["人数"]) == {1}
    assert first.iloc[1]["勤務時刻"] == "08:00–翌日 00:00"
    roster = table(at, "職員ID", "氏名", "役職")
    assert (roster == "日勤+準夜").to_numpy().sum() == 1


def test_ui_rejects_invalid_merge_without_result():
    at = three_shift_app()
    at.session_state.merge_source = pd.DataFrame([
        {"日付": START.isoformat(), "統合するシフト": ["日勤"], "休憩（分）": None},
    ])
    at.session_state.merge_revision += 1
    at.run()
    generate(at)
    assert "schedule" not in at.session_state
    assert any("2つ以上" in error.value for error in at.error)
