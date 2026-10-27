"""曜日固定・複数シフト回避を、画面操作・CSV取込から生成まで検証する。"""

import json
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from optimizer import WeeklyAssignment, audit_schedule


START = date(2026, 1, 26)  # 月曜。8日間なら月・週の境界と翌月の月曜を含む。
WEEKLY_COLUMNS = ["職員ID", "曜日", "シフト"]
PREFERENCE_COLUMNS = ["職員ID", "回避シフト", "重み"]
DOWNLOADS = {
    "シフト表 CSV", "日別配置 CSV", "職員別集計 CSV", "月別労働時間 CSV",
    "個別勤務明細 CSV", "ペア配慮結果 CSV", "曜日固定結果 CSV",
    "シフト回避結果 CSV", "作成条件を保存（JSON）",
}


def small_app(size=3, days=1, pattern="日勤のみ"):
    # appをimportすると収集時に画面コードが動くため、必ずAppTestで実行する。
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=30).run()
    assert not at.exception
    at.date_input[0].set_value(START)
    next(n for n in at.number_input if n.label == "作成日数").set_value(days)
    next(n for n in at.number_input if n.label == "計算時間の上限（秒）").set_value(5)
    at.selectbox(key="shift_pattern").select(pattern)
    at.session_state.staff_source = at.session_state.staff_source.iloc[:size].copy()
    at.session_state.editor_revision += 1
    at.run()
    assert not at.exception
    return at


def inject(at, weekly=None, preferences=None):
    # CSV取込と同じ正規化済みソースから、新しいキーで各ウィジェットを構築する。
    if weekly is not None:
        at.session_state.weekly_source = pd.DataFrame(weekly, columns=WEEKLY_COLUMNS)
    if preferences is not None:
        at.session_state.preference_source = pd.DataFrame(preferences, columns=PREFERENCE_COLUMNS)
    at.session_state.weekly_revision += 1
    at.run()
    assert not at.exception
    return at


def fixed(pid="S01", weekday="月", shift="日勤"):
    return {"職員ID": pid, "曜日": weekday, "シフト": shift}


def preference(pid="S01", shifts=("日勤",), weight=1):
    return {"職員ID": pid, "回避シフト": json.dumps(list(shifts), ensure_ascii=False), "重み": weight}


def generate(at):
    next(b for b in at.button if b.label == "シフトを作成").click().run()
    assert not at.exception
    return at


def table(at, *columns):
    matches = [f.value for f in at.dataframe if set(columns) <= set(f.value.columns)]
    assert len(matches) == 1
    return matches[0]


def labels(at):
    return {element.proto.label for element in at.get("download_button")}


def solution(at, count):
    assert at.success
    _, result, people, settings = at.session_state.schedule
    assert result.status == "optimal", result.messages
    assert len(result.assignments) == count
    assert 1 <= settings.days <= 8
    assert settings.time_limit_seconds == 5
    assert audit_schedule(people, settings, result.assignments) == []
    assert DOWNLOADS <= labels(at)
    return result, people, settings


def no_results(at):
    assert not at.success
    assert not (DOWNLOADS & labels(at))
    assert not any(m.label in {"回避希望シフトへの配置", "配慮ペアの同日・同シフト配置"} for m in at.metric)
    assert not any("割当シフト" in f.value.columns or "実労働時間" in f.value.columns for f in at.dataframe)


def import_csv(at, kind, frame):
    content = frame.to_csv(index=False).encode("utf-8-sig")

    def upload(label, *_args, **_kwargs):
        return BytesIO(content) if label == f"{kind}CSV（UTF-8）" else None

    with patch("streamlit.file_uploader", side_effect=upload):
        at.run()
        next(b for b in at.button if b.label == f"CSVを{kind}に反映").click().run()
    assert not at.exception
    return at


def test_recurring_fixed_weekday_across_month_uses_configured_times_and_exports():
    at = inject(small_app(days=8), weekly=[fixed()])
    assert any("04" in tab.label and "曜日固定・シフト回避" in tab.label for tab in at.tabs)
    result, people, settings = solution(generate(at), 16)
    person = next(p for p in people if p.employee_id == "S01")
    assert person.weekly_assignments == (WeeklyAssignment(0, "日勤"),)
    required = {date(2026, 1, 26), date(2026, 2, 2)}
    assert required <= {a.day for a in result.assignments if a.employee_id == "S01" and a.shift == "日勤"}
    rows = table(at, "曜日", "割当シフト")
    assert rows.to_dict("records") == [
        {"職員ID": "S01", "日付": day.isoformat(), "曜日": "月", "固定シフト": "日勤", "割当シフト": "日勤"}
        for day in sorted(required)
    ]
    details = table(at, "日付", "実労働時間")
    assert set(details["勤務時刻"]) == {settings.shift_times[0].label}
    assert set(details["休憩（分）"]) == {60}
    assert set(details["実労働時間"]) == {8}


def test_fixed_does_not_make_other_weekdays_days_off():
    at = inject(small_app(size=2, days=2), weekly=[fixed()])
    result, _, _ = solution(generate(at), 4)
    assert {a.day for a in result.assignments if a.employee_id == "S01"} == {START, date(2026, 1, 27)}


def test_all_fixed_staff_force_placements_above_minimum():
    at = inject(small_app(), weekly=[fixed(pid) for pid in ("S01", "S02", "S03")])
    solution(generate(at), 3)


def test_fixed_conflicting_requested_day_off_is_infeasible():
    at = small_app()
    at.session_state.off_source = pd.DataFrame([{"職員ID": "S01", "日付": START.isoformat()}])
    at.session_state.editor_revision += 1
    inject(at, weekly=[fixed()])
    generate(at)
    assert at.session_state.schedule[1].status == "infeasible"
    assert any("曜日固定" in message for message in at.session_state.schedule[1].messages)
    no_results(at)


@pytest.mark.parametrize("rows, message", [
    ([fixed(shift="未知")], "シフト"),
    ([fixed(), fixed()], "重複"),
    ([fixed(), fixed(shift="深夜")], "重複"),
    ([fixed(pid="UNKNOWN")], "登録済み"),
    ([fixed(weekday="月曜")], "曜日"),
    ([fixed(weekday="2026-01-26")], "曜日"),
    ([fixed(weekday="0")], "曜日"),
    ([fixed(weekday="")], "曜日"),
])
def test_invalid_weekly_source_rejected_on_generation(rows, message):
    at = inject(small_app(), weekly=rows)
    generate(at)
    assert at.error
    assert message in " / ".join(e.value for e in [*at.error, *at.markdown])
    if "schedule" in at.session_state:
        assert at.session_state.schedule[1].status == "invalid"
    no_results(at)


@pytest.mark.parametrize("size, count", [(3, 0), (2, 1)])
def test_avoidable_and_unavoidable_shift_is_soft(size, count):
    at = inject(small_app(size=size), preferences=[preference(weight=17)])
    result, people, _ = solution(generate(at), 2)
    person = next(p for p in people if p.employee_id == "S01")
    assert person.avoided_shifts == frozenset({"日勤"})
    assert person.shift_avoidance_weight == 17
    assert sum(a.employee_id == "S01" for a in result.assignments) == count
    assert next(m for m in at.metric if m.label == "回避希望シフトへの配置").value == f"{count} 回"
    rows = table(at, "回避シフト", "日付")
    assert len(rows) == count
    if count:
        assert rows.to_dict("records") == [{"職員ID": "S01", "日付": START.isoformat(), "回避シフト": "日勤", "重み": 17}]
        assert any("回避希望シフトへの配置が残って" in w.value for w in at.warning)


def test_actual_multiselect_multiple_choices_change_and_hard_fixed_wins_with_csv_payloads():
    at = inject(small_app(size=7, pattern="三交代"), weekly=[fixed(shift="深夜")])
    rev = at.session_state.weekly_revision
    widget = at.multiselect(key=f"avoid_shifts_{rev}_S01")
    widget.select("深夜").select("準夜")
    at.number_input(key=f"avoid_weight_{rev}_S01").set_value(100)
    at.run()
    assert not at.exception
    assert at.multiselect(key=f"avoid_shifts_{rev}_S01").value == ["深夜", "準夜"]
    assert json.loads(at.session_state.preference_source.iloc[0]["回避シフト"]) == ["深夜", "準夜"]
    payloads = {}
    original = st.download_button

    def capture(label, data, *args, **kwargs):
        payloads[label] = data
        return original(label, data, *args, **kwargs)

    with patch("streamlit.download_button", side_effect=capture):
        generate(at)
    result, people, _ = solution(at, 6)
    person = next(p for p in people if p.employee_id == "S01")
    assert person.avoided_shifts == frozenset({"深夜", "準夜"})
    assert person.shift_avoidance_weight == 100
    assert any(a.employee_id == "S01" and a.shift == "深夜" for a in result.assignments)
    assert next(m for m in at.metric if m.label == "回避希望シフトへの配置").value == "1 回"
    for label, columns in (("曜日固定結果 CSV", ["曜日", "割当シフト"]),
                           ("シフト回避結果 CSV", ["回避シフト", "日付"]),
                           ("個別勤務明細 CSV", ["日付", "実労働時間"])):
        assert payloads[label].startswith(b"\xef\xbb\xbf")
        pd.testing.assert_frame_equal(pd.read_csv(BytesIO(payloads[label])), table(at, *columns), check_dtype=False)
    exported = json.loads(payloads["作成条件を保存（JSON）"])
    assert exported["曜日固定"] == [fixed(shift="深夜")]
    assert json.loads(exported["シフト回避"][0]["回避シフト"]) == ["深夜", "準夜"]
    at.multiselect(key=f"avoid_shifts_{rev}_S01").unselect("深夜").select("日勤").run()
    assert not at.exception
    assert any("入力条件が変更" in w.value for w in at.warning)
    no_results(at)
    solution(generate(at), 6)
    assert next(p for p in at.session_state.schedule[2] if p.employee_id == "S01").avoided_shifts == frozenset({"日勤", "準夜"})
    assert table(at, "回避シフト", "日付").empty


@pytest.mark.parametrize("pair_weight, shift_weight, chosen", [(3, 2, "S03"), (3, 4, "S02")])
def test_pair_and_shift_costs_combine_before_fairness(pair_weight, shift_weight, chosen):
    at = small_app()
    at.session_state.pair_source = pd.DataFrame([
        {"職員ID A": "S01", "職員ID B": "S02", "重み": pair_weight}
    ])
    at.session_state.condition_revision += 1
    inject(at, weekly=[fixed()], preferences=[preference("S03", weight=shift_weight)])
    result, _, _ = solution(generate(at), 2)
    assert {a.employee_id for a in result.assignments} == {"S01", chosen}
    assert len(table(at, "職員ID A", "日付")) == (chosen == "S02")
    assert len(table(at, "回避シフト", "日付")) == (chosen == "S03")


@pytest.mark.parametrize("change", ["weekly", "preferences", "weight"])
def test_input_updates_hide_all_previous_results_and_downloads(change):
    at = inject(small_app(), weekly=[fixed()], preferences=[preference()])
    solution(generate(at), 2)
    previous = at.session_state.schedule[0]
    if change == "weekly":
        inject(at, weekly=[fixed("S02")])
    elif change == "preferences":
        inject(at, preferences=[preference("S02")])
    else:
        at.number_input(key=f"avoid_weight_{at.session_state.weekly_revision}_S01").set_value(9).run()
    assert not at.exception
    assert any("入力条件が変更" in w.value for w in at.warning)
    no_results(at)
    solution(generate(at), 2)
    assert at.session_state.schedule[0] != previous


@pytest.mark.parametrize("kind", ["曜日固定", "シフト回避"])
def test_csv_import_buttons_preserve_other_sources_and_rebuild_widgets(kind):
    at = inject(small_app(), weekly=[fixed()], preferences=[preference("S02", weight=2)])
    at.session_state.pair_source = pd.DataFrame([{"職員ID A": "S01", "職員ID B": "S03", "重み": 3}])
    at.session_state.work_time_source = pd.DataFrame([
        {"職員ID": "S01", "日付": "", "シフト": "日勤", "開始": "08:00", "終了": "18:00", "翌日終了": False, "休憩（分）": 60}
    ])
    at.session_state.condition_revision += 1
    at.run()
    solution(generate(at), 2)
    before = {name: at.session_state[name].copy(deep=True) for name in (
        "staff_source", "off_source", "pair_source", "work_time_source", "weekly_source", "preference_source")}
    revision = at.session_state.weekly_revision
    source = (pd.DataFrame([fixed("S03")], columns=WEEKLY_COLUMNS) if kind == "曜日固定" else
              pd.DataFrame([preference("S02", weight=17)], columns=PREFERENCE_COLUMNS))
    import_csv(at, kind, source)
    assert at.session_state.weekly_revision == revision + 1
    for name, old in before.items():
        if name != ("weekly_source" if kind == "曜日固定" else "preference_source"):
            pd.testing.assert_frame_equal(at.session_state[name], old)
    at.run()
    assert not at.exception
    assert any("入力条件が変更" in w.value for w in at.warning)
    no_results(at)
    rev = at.session_state.weekly_revision
    assert at.multiselect(key=f"avoid_shifts_{rev}_S02").value == ["日勤"]
    assert at.number_input(key=f"avoid_weight_{rev}_S02").value == (2 if kind == "曜日固定" else 17)
    assert at.session_state.preference_source["重み"].dtype.kind in "iu"
    _, people, _ = solution(generate(at), 2)
    assert next(p for p in people if p.employee_id == ("S03" if kind == "曜日固定" else "S01")).weekly_assignments == (WeeklyAssignment(0, "日勤"),)


@pytest.mark.parametrize("kind, frame, message", [
    ("曜日固定", pd.DataFrame([fixed(pid="UNKNOWN")]), "登録済み"),
    ("曜日固定", pd.DataFrame([fixed(weekday="2026-01-26")]), "曜日"),
    ("曜日固定", pd.DataFrame([fixed(weekday="月曜")]), "曜日"),
    ("曜日固定", pd.DataFrame([fixed(shift="未知")]), "シフト"),
    ("曜日固定", pd.DataFrame([fixed(), fixed()]), "重複"),
    ("曜日固定", pd.DataFrame([{"職員ID": "S01", "日付": "2026-01-26", "シフト": "日勤"}]), "列"),
    ("シフト回避", pd.DataFrame([preference("UNKNOWN")]), "未登録"),
    ("シフト回避", pd.DataFrame([preference(), preference()]), "重複"),
    ("シフト回避", pd.DataFrame([preference(shifts=("未知",))]), "シフト"),
    *[("シフト回避", pd.DataFrame([{"職員ID": "S01", "回避シフト": raw, "重み": 1}]), "JSON配列")
    for raw in ('[', '"日勤"', '{}', 'false', '[1]', '["日勤", "日勤"]')],
    *[("シフト回避", pd.DataFrame([preference(weight=weight)]), "1〜100")
    for weight in (0, 101, "1.5", "Infinity")],
])
def test_invalid_csv_import_is_atomic_and_shows_validation_error(kind, frame, message):
    at = inject(small_app(), weekly=[fixed()], preferences=[preference("S02")])
    before = {name: at.session_state[name].copy(deep=True) for name in ("weekly_source", "preference_source")}
    revision = at.session_state.weekly_revision
    import_csv(at, kind, frame)
    assert any("読み込めませんでした" in e.value and message in e.value for e in at.error)
    assert at.session_state.weekly_revision == revision
    for name, old in before.items():
        pd.testing.assert_frame_equal(at.session_state[name], old)
    no_results(at)


def test_deleted_employee_preference_is_not_silently_discarded_on_generation():
    at = inject(small_app(), preferences=[preference("UNKNOWN")])
    generate(at)
    assert any("未登録" in e.value for e in at.error)
    no_results(at)