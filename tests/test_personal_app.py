"""配慮ペア・個別勤務時刻を実際の生成画面に通す回帰テスト。"""

from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


START = date(2026, 1, 1)
PAIR_COLUMNS = ["職員ID A", "職員ID B", "重み"]
TIME_COLUMNS = ["職員ID", "日付", "シフト", "開始", "終了", "翌日終了", "休憩（分）"]
RESULT_DOWNLOADS = {
    "シフト表 CSV", "日別配置 CSV", "職員別集計 CSV", "月別労働時間 CSV",
    "個別勤務明細 CSV", "ペア配慮結果 CSV", "作成条件を保存（JSON）",
}


def small_app(size=3):
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=30).run()
    assert not at.exception
    at.date_input[0].set_value(START)
    next(n for n in at.number_input if n.label == "作成日数").set_value(1)
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(5)
    at.selectbox(key="shift_pattern").select("日勤のみ")
    at.session_state.staff_source = at.session_state.staff_source.iloc[:size].copy()
    at.session_state.editor_revision += 1
    at.run()
    assert not at.exception
    return at


def inject(at, pairs=None, times=None):
    # CSV取込後と同じDataFrameを、新しいエディターキーで画面に反映する。
    if pairs is not None:
        at.session_state.pair_source = pd.DataFrame(pairs, columns=PAIR_COLUMNS)
    if times is not None:
        at.session_state.work_time_source = pd.DataFrame(times, columns=TIME_COLUMNS)
    at.session_state.condition_revision += 1
    at.run()
    assert not at.exception
    return at


def generate(at):
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    return at


def work_time(pid="S01", **changes):
    return {"職員ID": pid, "日付": "", "シフト": "日勤", "開始": "09:00",
            "終了": "18:00", "翌日終了": False, "休憩（分）": 60, **changes}


def table(at, *columns):
    matches = [f.value for f in at.dataframe if set(columns) <= set(f.value.columns)]
    assert len(matches) == 1, [list(frame.columns) for frame in matches]
    return matches[0]


def download_labels(at):
    return {element.proto.label for element in at.get("download_button")}


def assert_solution(at, count):
    assert at.success
    _, result, people, settings = at.session_state.schedule
    assert result.status == "optimal", result.messages
    assert len(result.assignments) == count
    assert settings.days == 1
    assert settings.time_limit_seconds == 5
    assert RESULT_DOWNLOADS <= download_labels(at)
    return result, people, settings


def assert_no_results(at):
    assert not at.success
    assert not (RESULT_DOWNLOADS & download_labels(at))
    assert not any("実労働時間" in f.value.columns for f in at.dataframe)


@pytest.mark.parametrize("weight, expected", [(None, 1), ("", 1), (17, 17), (100, 100)])
def test_unavoidable_pair_is_soft_and_exports_default_or_explicit_weight(weight, expected):
    at = small_app(2)
    inject(at, pairs=[{"職員ID A": "S01", "職員ID B": "S02", "重み": weight}])
    result, people, settings = assert_solution(generate(at), 2)
    assert all(person.is_leader for person in people)
    assert {a.employee_id for a in result.assignments} == {"S01", "S02"}
    assert settings.avoid_pairs[0].weight == expected
    assert any("ソフト制約" in warning.value for warning in at.warning)
    metric = next(m for m in at.metric if m.label == "配慮ペアの同日・同シフト配置")
    assert metric.value == "1 回"
    assert metric.delta == f"重み付き合計 {expected}"
    conflicts = table(at, "職員ID A", "職員ID B", "日付")
    assert conflicts.to_dict("records") == [
        {"職員ID A": "S01", "職員ID B": "S02", "日付": START.isoformat(),
         "シフト": "日勤", "重み": expected},
    ]


def test_avoidable_pair_with_three_staff_has_no_conflicts():
    at = small_app()
    inject(at, pairs=[{"職員ID A": "S01", "職員ID B": "S02", "重み": 100}])
    result, _, _ = assert_solution(generate(at), 2)
    ids = {a.employee_id for a in result.assignments}
    assert "S03" in ids
    assert len(ids & {"S01", "S02"}) == 1
    assert table(at, "職員ID A", "職員ID B", "日付").empty
    assert any("配置されていません" in success.value for success in at.success)


@pytest.mark.parametrize("kind", ["回避ペア", "個別勤務時刻"])
def test_csv_import_restores_editor_types_and_preserves_other_inputs(kind):
    at = small_app()
    inject(at, pairs=[{"職員ID A": "S01", "職員ID B": "S02", "重み": 2}],
           times=[work_time("S03", **{"開始": "10:00", "終了": "15:00"})])
    source = (pd.DataFrame([{"職員ID A": "S01", "職員ID B": "S03", "重み": 17}])
              if kind == "回避ペア" else pd.DataFrame([work_time("S03", **{"開始": "11:00", "終了": "14:00"})]))
    content = source.to_csv(index=False).encode("utf-8-sig")

    def upload(label, *_args, **_kwargs):
        del _args, _kwargs
        return BytesIO(content) if label == f"{kind}CSV（UTF-8）" else None

    with patch("streamlit.file_uploader", side_effect=upload):
        at.run()
        next(b for b in at.button if b.label == f"CSVを{kind}に反映").click().run()
    assert not at.exception
    at.run()
    assert not at.exception
    assert at.session_state.work_time_source["翌日終了"].dtype.kind == "b"
    assert at.session_state.work_time_source["休憩（分）"].dtype.kind in "iu"
    assert at.session_state.pair_source["重み"].dtype.kind in "iu"
    if kind == "回避ペア":
        assert at.session_state.pair_source.iloc[0]["重み"] == 17
        assert at.session_state.work_time_source.iloc[0]["開始"] == "10:00"
    else:
        assert at.session_state.work_time_source.iloc[0]["開始"] == "11:00"
        assert at.session_state.pair_source.iloc[0]["重み"] == 2
    generate(at)
    assert at.session_state.schedule[1].has_solution


def test_weights_choose_least_cost_pair_through_ui():
    at = small_app()
    inject(at, pairs=[
        {"職員ID A": "S01", "職員ID B": "S02", "重み": 100},
        {"職員ID A": "S01", "職員ID B": "S03", "重み": 2},
        {"職員ID A": "S02", "職員ID B": "S03", "重み": 1},
    ])
    result, _, _ = assert_solution(generate(at), 2)
    assert {a.employee_id for a in result.assignments} == {"S02", "S03"}
    assert table(at, "職員ID A", "職員ID B", "日付")["重み"].tolist() == [1]


@pytest.mark.parametrize("precedence", ["recurring", "dated_first", "dated_last"])
def test_individual_times_precedence_coverage_and_actual_hour_exports(precedence):
    at = small_app()
    recurring = work_time("S03", **{"開始": "10:00", "終了": "15:00"})
    dated = work_time("S03", **{"日付": START.isoformat(), "開始": "11:00", "終了": "14:00", "休憩（分）": 30})
    short_rows = [recurring] if precedence == "recurring" else (
        [dated, recurring] if precedence == "dated_first" else [recurring, dated])
    hours, label, pause = (4, "10:00–15:00", 60) if precedence == "recurring" else (2.5, "11:00–14:00", 30)
    staff = at.session_state.staff_source.copy()
    # 1月の1日分に日割りすると短時間勤務1回と完全一致。登録だけでは割当を強制しない。
    staff.loc[2, "月間実労働時間プリセット"] = "カスタム"
    staff.loc[2, "月間実労働時間（時間）"] = hours * 31
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    inject(at, times=[
        work_time("S01", **{"開始": "08:00", "休憩（分）": 90}),
        work_time("S02", **{"終了": "19:00"}), *short_rows,
    ])
    result, _, _ = assert_solution(generate(at), 3)
    assert {a.employee_id for a in result.assignments} == {"S01", "S02", "S03"}
    details = table(at, "職員ID", "日付", "実労働時間").set_index("職員ID")
    expected = {"S01": (8.5, "08:00–18:00", 90), "S02": (9, "09:00–19:00", 60),
                "S03": (hours, label, pause)}
    summary = table(at, "勤務日数").set_index("職員ID")
    monthly = table(at, "対象期間の指定実労働時間").set_index("職員ID")
    roster = table(at, "役職").set_index("職員ID")
    day_column = "01/01(木)"
    for pid, (actual, timing, rest) in expected.items():
        assert details.loc[pid, "勤務時刻"] == timing
        assert details.loc[pid, "休憩（分）"] == rest
        assert details.loc[pid, "実労働時間"] == actual
        assert summary.loc[pid, "勤務時間"] == actual
        assert summary.loc[pid, "勤務日数"] == 1
        assert monthly.loc[pid, "実労働時間"] == actual
        assert roster.loc[pid, day_column] == f"日勤 {timing}（休憩{rest}分）"
    assert monthly.loc["S03", "対象期間の指定実労働時間"] == hours
    assert monthly.loc["S03", "指定との差（時間）"] == 0
    assert monthly.loc[["S01", "S02"], "対象期間の指定実労働時間"].isna().all()
    coverage = table(at, "時間帯別最少人数")
    assert coverage["人数"].tolist() == [3]
    assert coverage["時間帯別最少人数"].tolist() == [2]
    assert coverage["時間帯別最少主任以上"].tolist() == [2]
    # 日別配置の勤務時間は標準値。個別明細・集計とは区別する。
    assert coverage["勤務時刻"].tolist() == ["09:00–18:00"]
    assert coverage["勤務時間"].tolist() == [8]
    assert label in coverage.iloc[0]["担当者"]


def test_override_alone_does_not_force_assignment():
    at = small_app()
    staff = at.session_state.staff_source.copy()
    staff.loc[2, "月間実労働時間プリセット"] = "カスタム"
    staff.loc[2, "月間実労働時間（時間）"] = 0
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    inject(at, times=[work_time("S03", **{"開始": "10:00", "終了": "14:00"})])
    result, _, _ = assert_solution(generate(at), 2)
    assert {a.employee_id for a in result.assignments} == {"S01", "S02"}
    assert "S03" not in set(table(at, "職員ID", "日付", "実労働時間")["職員ID"])
    assert table(at, "役職").set_index("職員ID").loc["S03", "01/01(木)"] == "休"


@pytest.mark.parametrize("source", ["pairs", "times"])
def test_changed_personal_conditions_hide_all_previous_results(source):
    at = small_app()
    inject(at, pairs=[{"職員ID A": "S01", "職員ID B": "S02", "重み": 1}],
           times=[work_time("S03")])
    assert_solution(generate(at), 2)
    saved_fingerprint = at.session_state.schedule[0]
    if source == "pairs":
        inject(at, pairs=[{"職員ID A": "S01", "職員ID B": "S02", "重み": 9}])
    else:
        inject(at, times=[work_time("S03", **{"開始": "08:00"})])
    assert any("入力条件が変更" in warning.value for warning in at.warning)
    assert_no_results(at)
    assert_solution(generate(at), 2)
    assert at.session_state.schedule[0] != saved_fingerprint


@pytest.mark.parametrize("mode, expected_status", [("各シフトに1人以上", "infeasible"),
                                                   ("1日のどこかに1人以上", "optimal")])
def test_partial_leader_needs_segment_coverage_only_in_each_shift_mode(mode, expected_status):
    at = small_app()
    staff = at.session_state.staff_source.copy()
    staff["主任以上"] = [True, False, False]
    at.session_state.staff_source = staff
    at.session_state.editor_revision += 1
    at.radio[0].set_value(mode)
    inject(at, times=[work_time("S01", **{"開始": "10:00", "終了": "14:00", "休憩（分）": 0})])
    generate(at)
    assert at.session_state.schedule[1].status == expected_status
    if expected_status == "optimal":
        assert_solution(at, 3)
        assert table(at, "時間帯別最少人数")["時間帯別最少主任以上"].tolist() == [0]
    else:
        assert at.error
        assert_no_results(at)


def test_two_category_assignments_do_not_mean_full_temporal_coverage():
    at = small_app(2)
    inject(at, times=[work_time("S01", **{"開始": "10:00"})])
    generate(at)
    assert at.session_state.schedule[1].status == "infeasible"
    assert any("時間帯の最低人数" in message for message in at.session_state.schedule[1].messages)
    assert_no_results(at)


@pytest.mark.parametrize("pairs, message", [
    ([{"職員ID A": "UNKNOWN", "職員ID B": "S02", "重み": 1}], "未登録"),
    ([{"職員ID A": "S01", "職員ID B": "S01", "重み": 1}], "同じ職員"),
    ([{"職員ID A": "S01", "職員ID B": "S02", "重み": 1}] * 2, "重複"),
    ([{"職員ID A": "S01", "職員ID B": "S02", "重み": 1},
      {"職員ID A": "S02", "職員ID B": "S01", "重み": 2}], "逆順"),
    ([{"職員ID A": "S01", "職員ID B": "S02", "重み": "1.5"}], "整数"),
    ([{"職員ID A": "S01", "職員ID B": "S02", "重み": 0}], "1〜100"),
    ([{"職員ID A": "S01", "職員ID B": "S02", "重み": 101}], "1〜100"),
])
def test_invalid_pairs_are_rejected_by_generation(pairs, message):
    at = inject(small_app(), pairs=pairs)
    generate(at)
    assert at.error
    displayed = " / ".join(e.value for e in [*at.error, *at.markdown])
    assert message in displayed
    if "schedule" in at.session_state:
        assert at.session_state.schedule[1].status == "invalid"
    assert_no_results(at)


@pytest.mark.parametrize("changes, message", [
    ({"職員ID": "UNKNOWN"}, "職員一覧にありません"),
    ({"開始": ""}, "HH:MM"),
    ({"終了": None}, "HH:MM"),
    ({"開始": "9:00"}, "HH:MM"),
    ({"開始": "24:00"}, "HH:MM"),
    ({"終了": "18:60"}, "HH:MM"),
    ({"日付": "2026-02-30"}, "YYYY-MM-DD"),
    ({"日付": "2026-01-02"}, "期間外"),
    ({"シフト": "未知の勤務"}, "シフト一覧にありません"),
    ({"シフト": ""}, "シフト一覧にありません"),
    ({"開始": "20:00", "終了": "08:00", "翌日終了": False}, "勤務長"),
    ({"開始": "09:00", "終了": "10:00", "翌日終了": True}, "勤務長"),
    ({"休憩（分）": 540}, "休憩"),
    ({"休憩（分）": "1.5"}, "整数分"),
    ({"翌日終了": "maybe"}, "TRUE/FALSE"),
])
def test_invalid_individual_times_are_rejected_by_generation(changes, message):
    at = inject(small_app(), times=[work_time(**changes)])
    generate(at)
    assert at.error
    displayed = " / ".join(e.value for e in [*at.error, *at.markdown])
    assert message in displayed
    if "schedule" in at.session_state:
        assert at.session_state.schedule[1].status == "invalid"
    assert_no_results(at)


@pytest.mark.parametrize("day", ["", START.isoformat()])
def test_duplicate_recurring_or_dated_overrides_are_rejected(day):
    at = inject(small_app(), times=[work_time(**{"日付": day})] * 2)
    generate(at)
    assert at.session_state.schedule[1].status == "invalid"
    assert any("重複" in message for message in at.session_state.schedule[1].messages)
    assert_no_results(at)


@pytest.mark.parametrize("end, label, hours", [("00:00", "09:00–翌日 00:00", 14),
                                             ("08:00", "09:00–翌日 08:00", 22)])
def test_valid_overnight_clock_uses_actual_duration(end, label, hours):
    at = small_app(2)
    inject(at, times=[work_time(**{"終了": end, "翌日終了": True})])
    assert_solution(generate(at), 2)
    details = table(at, "職員ID", "日付", "実労働時間").set_index("職員ID")
    assert details.loc["S01", "勤務時刻"] == label
    assert details.loc["S01", "実労働時間"] == hours