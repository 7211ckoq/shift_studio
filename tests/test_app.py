"""Streamlit の実行エンジンを使った画面の回帰テスト。"""

from datetime import date, time, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays


def app():
    return all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()


def click_generate(at):
    next(button for button in at.button if button.label == "シフトを作成").click().run()
    return at


def test_initial_screen_and_generation():
    at = app()
    assert not at.exception
    staff_columns = set(at.session_state.staff_source.columns)
    assert {"月間実労働時間プリセット", "月間実労働時間（時間）", "月間上限（時間）"} <= staff_columns
    assert not any("目標" in column or "厳守" in column for column in staff_columns)
    staff_editor = next(frame.value for frame in at.dataframe if "主任以上" in frame.value.columns)
    assert set(staff_editor.columns) == staff_columns
    click_generate(at)
    assert not at.exception
    assert at.success
    result = at.session_state.schedule[1]
    assert result.has_solution
    assert len(result.assignments) == 14 * 6
    monthly = next(frame.value for frame in at.dataframe if "実労働時間" in frame.value.columns)
    assert set(monthly.columns) == {
        "職員ID", "氏名", "月", "対象日数", "実労働時間", "対象期間の指定実労働時間",
        "指定との差（時間）", "月間上限（時間）", "固定シフト",
    }
    assert monthly["対象期間の指定実労働時間"].isna().all()
    assert monthly["指定との差（時間）"].isna().all()


def test_changed_settings_hide_old_results():
    at = click_generate(app())
    next(number for number in at.number_input if number.label == "各シフトの最低人数 N").set_value(3).run()
    assert not at.exception
    assert any("入力条件が変更" in warning.value for warning in at.warning)
    assert not at.success


def test_missing_leaders_shows_no_schedule():
    at = app()
    frame = at.session_state.staff_source.copy()
    frame["主任以上"] = False
    at.session_state.staff_source = frame
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.error
    assert at.session_state.schedule[1].status == "infeasible"
    assert not at.success


def test_requested_day_off_and_out_of_range_validation():
    at = app()
    start = at.date_input[0].value[0]
    at.session_state.off_source = pd.DataFrame([{"職員ID": "S01", "日付": start.isoformat()}])
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    result = at.session_state.schedule[1]
    assert result.has_solution
    assert not any(item.employee_id == "S01" and item.day == start for item in result.assignments)
    at.session_state.off_source = pd.DataFrame([{"職員ID": "S01", "日付": (start + timedelta(days=99)).isoformat()}])
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert any("期間外" in error.value for error in at.error)
    assert "schedule" not in at.session_state


def test_shift_time_changes_hide_result_and_update_duration_exports():
    at = click_generate(app())
    at.time_input(key="end_日勤").set_value(time(16, 30)).run()
    assert not at.exception
    assert not at.success
    assert any("入力条件が変更" in warning.value for warning in at.warning)
    click_generate(at)
    assert not at.exception
    assert at.success
    settings = at.session_state.schedule[3]
    assert settings.shift_times[1].duration_minutes == 510
    # 出力表の時間も固定8時間ではなく、各勤務帯の長さから計算される。
    summary = next(frame.value for frame in at.dataframe if "勤務日数" in frame.value.columns)
    for _, row in summary.iterrows():
        assert row["勤務時間"] == row["深夜"] * 8 + row["日勤"] * 8.5 + row["準夜"] * 8
    coverage = next(frame.value for frame in at.dataframe if "勤務時刻" in frame.value.columns)
    assert set(coverage.loc[coverage["シフト"] == "日勤", "勤務時刻"]) == {"08:00–16:30"}


def test_overnight_settings_and_invalid_time_feedback():
    at = app()
    at.time_input(key="end_準夜").set_value(time(0, 30)).run()
    click_generate(at)
    assert not at.exception
    assert at.success
    assert at.session_state.schedule[3].shift_times[2].end_minute == 1470
    at.checkbox(key="next_day_準夜").uncheck().run()
    click_generate(at)
    assert not at.exception
    assert at.error
    assert not at.success
    assert at.session_state.schedule[1].status == "invalid"


def test_two_shift_and_single_shift_presets():
    for pattern, names in (("二交代", {"日勤", "夜勤"}), ("日勤のみ", {"日勤"})):
        at = app()
        at.selectbox(key="shift_pattern").select(pattern).run()
        click_generate(at)
        assert not at.exception
        result = at.session_state.schedule[1]
        assert result.has_solution
        assert {a.shift for a in result.assignments} == names
        assert len(result.assignments) == 14 * len(names) * 2


def test_custom_four_shifts_and_renaming():
    at = app()
    at.selectbox(key="shift_pattern").select("カスタム").run()
    at.number_input(key="shift_count").set_value(4).run()
    at.text_input(key="custom_name_3").set_value("早番").run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].has_solution
    assert set(at.session_state.schedule[3].shift_names) == {"深夜", "日勤", "準夜", "早番"}


def test_fixed_employee_working_time_preset_and_cap_are_used():
    at = app()
    at.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=31 - 1))).run()
    staff = at.session_state.staff_source.copy()
    staff.loc[0, "固定シフト"] = "深夜"
    staff.loc[0, "月間実労働時間プリセット"] = "80時間"
    staff.loc[0, "月間上限（時間）"] = 40
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].status == "invalid"  # 指定時間＞上限は入力エラー。
    staff.loc[0, "月間上限（時間）"] = 80
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    result = at.session_state.schedule[1]
    assert result.has_solution
    assignments = [a for a in result.assignments if a.employee_id == "S01"]
    assert len(assignments) == 10  # 1月全体なら80時間＝休憩控除後8時間×10勤務。
    assert all(a.shift == "深夜" for a in assignments)
    monthly = next(frame.value for frame in at.dataframe if "実労働時間" in frame.value.columns)
    row = monthly.loc[monthly["職員ID"] == "S01"].iloc[0]
    assert row["実労働時間"] == 80
    assert row["対象期間の指定実労働時間"] == 80
    assert row["指定との差（時間）"] == 0
    assert row["月間上限（時間）"] == 80
    assert row["固定シフト"] == "深夜"
    assert "目標厳守" not in monthly.columns


def test_break_changes_result_and_deducts_time():
    at = click_generate(app())
    at.number_input(key="break_日勤").set_value(60).run()
    assert any("入力条件が変更" in warning.value for warning in at.warning)
    assert not at.success
    click_generate(at)
    assert not at.exception
    summary = next(frame.value for frame in at.dataframe if "勤務日数" in frame.value.columns)
    for _, row in summary.iterrows():
        assert row["勤務時間"] == row["深夜"] * 8 + row["日勤"] * 7 + row["準夜"] * 8


def test_renamed_or_removed_fixed_shift_is_not_silently_ignored():
    at = app()
    staff = at.session_state.staff_source.copy()
    staff.loc[0, "固定シフト"] = "深夜"
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.selectbox(key="shift_pattern").select("日勤のみ").run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].status == "invalid"


def test_multiple_requested_days_preserved_by_parser():
    at = app()
    start = at.date_input[0].value[0]
    at.session_state.off_source = pd.DataFrame([
        {"職員ID": "S01", "日付": start.isoformat()},
        {"職員ID": "S01", "日付": (start + timedelta(days=1)).isoformat()},
        {"職員ID": "S02", "日付": (start + timedelta(days=2)).isoformat()},
    ])
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].has_solution
    people = at.session_state.schedule[2]
    assert people[0].days_off == frozenset({start, start + timedelta(days=1)})
    assert people[1].days_off == frozenset({start + timedelta(days=2)})


def test_custom_monthly_working_time_with_exact_matching():
    at = app()
    at.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=1 - 1))).run()
    at.selectbox(key="shift_pattern").select("日勤のみ").run()
    staff = at.session_state.staff_source.copy()
    staff.loc[0, "月間実労働時間プリセット"] = "カスタム"
    staff.loc[0, "月間実労働時間（時間）"] = 248
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].has_solution
    assert any(a.employee_id == "S01" for a in at.session_state.schedule[1].assignments)
    assert at.session_state.schedule[2][0].monthly_working_minutes == 248 * 60
    monthly = next(frame.value for frame in at.dataframe if "実労働時間" in frame.value.columns)
    row = monthly.loc[monthly["職員ID"] == "S01"].iloc[0]
    assert row["実労働時間"] == row["対象期間の指定実労働時間"] == 8
    assert row["指定との差（時間）"] == 0


def test_zero_custom_working_time_prohibits_assignment():
    at = app()
    staff = at.session_state.staff_source.copy()
    staff.loc[0, "月間実労働時間プリセット"] = "カスタム"
    staff.loc[0, "月間実労働時間（時間）"] = 0
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].has_solution
    assert at.session_state.schedule[2][0].monthly_working_minutes == 0
    assert all(a.employee_id != "S01" for a in at.session_state.schedule[1].assignments)
    monthly = next(frame.value for frame in at.dataframe if "実労働時間" in frame.value.columns)
    row = monthly.loc[monthly["職員ID"] == "S01"].iloc[0]
    assert row["実労働時間"] == row["対象期間の指定実労働時間"] == 0
    assert row["指定との差（時間）"] == 0


def test_nonmatching_working_time_is_infeasible_instead_of_approximated():
    at = app()
    at.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 1) + timedelta(days=1 - 1))).run()
    at.selectbox(key="shift_pattern").select("日勤のみ").run()
    staff = at.session_state.staff_source.copy()
    staff.loc[0, "月間実労働時間プリセット"] = "カスタム"
    staff.loc[0, "月間実労働時間（時間）"] = 240
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.session_state.schedule[1].status == "infeasible"
    assert not at.success
    assert not any("実労働時間" in frame.value.columns for frame in at.dataframe)


@pytest.mark.parametrize("legacy_column, value", [
    ("月間目標プリセット", "未設定"),
    ("月間目標（時間）", 0),
    ("目標厳守", False),
])
def test_legacy_staff_csv_columns_are_rejected_with_resave_guidance(legacy_column, value):
    at = click_generate(app())
    legacy = at.session_state.staff_source.copy()
    legacy[legacy_column] = value
    # AppTestのアップロード操作に依存せず、CSVを読み込んだ画面状態で同じ入力検証を通す。
    at.session_state.staff_source = pd.read_csv(StringIO(legacy.to_csv(index=False)))
    at.session_state.editor_revision += 1
    at.run()
    click_generate(at)
    assert not at.exception
    assert at.error
    message = " / ".join(error.value for error in at.error)
    assert "旧" in message
    assert "新形式" in message
    assert "保存" in message
    assert "schedule" not in at.session_state
    assert not at.success
    assert not any("実労働時間" in frame.value.columns for frame in at.dataframe)