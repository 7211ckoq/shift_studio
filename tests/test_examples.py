"""各入力表の記入例が、現在の期間・シフト名で表示されることのテスト。"""

from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays


def app():
    return all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()


def example_tables(at):
    return [table.value for table in at.table]


def test_every_input_has_example_with_current_period_and_shifts():
    at = app()
    at.date_input[0].set_value((date(2026, 11, 2), date(2026, 11, 15))).run()
    assert not at.exception
    tables = example_tables(at)
    # 必要人数・役職・職員・休み希望・同一シフトペア・回避ペア・個別勤務時刻・曜日固定・シフト回避・シフト統合
    assert len(tables) == 10
    assert sum("説明" in frame.columns for frame in tables) == 10
    days_off = next(frame for frame in tables if list(frame.columns) == ["職員ID", "日付", "説明"])
    assert days_off["日付"].iloc[0] == "2026-11-02"
    merges = next(frame for frame in tables if "統合するシフト" in frame.columns)
    assert merges["統合するシフト"].tolist() == ["深夜、日勤", "深夜、日勤、準夜"]
    work = next(frame for frame in tables if "開始" in frame.columns)
    assert work["シフト"].tolist() == ["深夜", "深夜"]
    # 記入例は編集表（data_editor）と混ざらない。
    assert not any("説明" in frame.value.columns for frame in at.dataframe)


def test_examples_follow_shift_pattern_without_mergeable_shifts():
    at = app()
    at.selectbox(key="shift_pattern").select("日勤のみ").run()
    assert not at.exception
    tables = example_tables(at)
    assert not any("統合するシフト" in frame.columns for frame in tables)
    assert any("統合の記入例はありません" in caption.value for caption in at.caption)
    weekly = next(frame for frame in tables if list(frame.columns) == ["職員ID", "曜日", "シフト", "説明"])
    assert set(weekly["シフト"]) == {"日勤"}
