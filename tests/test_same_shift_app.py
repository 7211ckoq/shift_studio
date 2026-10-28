"""同一シフトペアを実画面、CSV取込、結果変更検知に通すテスト。"""

from io import BytesIO
from unittest.mock import patch

import pandas as pd
import pytest

from test_personal_app import START, generate, small_app


COLUMNS = ["職員ID A", "職員ID B", "条件", "適用範囲", "優先度"]


def pair(**changes):
    return {"職員ID A": "S01", "職員ID B": "S02", "条件": "ハード（必須）",
            "適用範囲": "Aの勤務時にBを合わせる", "優先度": "普通", **changes}


def inject(at, rows):
    at.session_state.same_pair_source = pd.DataFrame(rows, columns=COLUMNS)
    at.session_state.condition_revision += 1
    at.run()
    assert not at.exception
    return at


def force_a(at):
    at.session_state.weekly_source = pd.DataFrame([
        {"職員ID": "S01", "曜日": "木", "シフト": "日勤"}
    ])
    at.session_state.weekly_revision += 1


@pytest.mark.parametrize("mode", ["ハード（必須）", "ソフト（できるだけ）"])
@pytest.mark.parametrize("direction", ["Aの勤務時にBを合わせる", "双方向（休日も合わせる）"])
def test_ui_generates_pair_with_selected_mode_and_direction(mode, direction):
    at = small_app()
    force_a(at)
    generate(inject(at, [pair(**{"条件": mode, "適用範囲": direction})]))
    _, result, _, settings = at.session_state.schedule
    assert result.status == "optimal"
    assert {a.employee_id for a in result.assignments} == {"S01", "S02"}
    assert settings.same_shift_pairs[0].mode == ("hard" if mode.startswith("ハード") else "soft")
    assert settings.same_shift_pairs[0].direction == ("both" if direction.startswith("双方向") else "a_to_b")
    assert next(m for m in at.metric if m.label == "同一シフトペアの未達勤務").value == "0 回"
    assert any(d.proto.label == "同一シフトペア結果 CSV" for d in at.get("download_button"))


@pytest.mark.parametrize("mode, status", [("ハード（必須）", "infeasible"), ("ソフト（できるだけ）", "optimal")])
def test_trainer_holiday_blocks_hard_but_soft_reports_unmet_work(mode, status):
    at = small_app()
    force_a(at)
    at.session_state.off_source = pd.DataFrame([{"職員ID": "S02", "日付": START.isoformat()}])
    at.session_state.editor_revision += 1
    generate(inject(at, [pair(**{"条件": mode, "優先度": "高め"})]))
    assert at.session_state.schedule[1].status == status
    if status == "optimal":
        metric = next(m for m in at.metric if m.label == "同一シフトペアの未達勤務")
        assert metric.value == "1 回"
        assert metric.delta == "最優先 0 / 高め 1 / 普通 0 / 低め 0"
        assert any("相手不在" in w.value for w in at.warning)
        details = next(f.value for f in at.dataframe if "対象職員ID" in f.value.columns)
        assert details.iloc[0]["対象職員ID"] == "S01"
        assert details.iloc[0]["相手のシフト"] == "休"
    else:
        assert at.error
        assert not at.success


def test_blank_optional_values_receive_defaults():
    at = small_app()
    force_a(at)
    generate(inject(at, [pair(**{"条件": None, "適用範囲": "", "優先度": None})]))
    configured = at.session_state.schedule[3].same_shift_pairs[0]
    assert (configured.mode, configured.direction, configured.weight) == ("hard", "a_to_b", 5)


@pytest.mark.parametrize("changes, message", [
    ({"職員ID B": "UNKNOWN"}, "未登録"),
    ({"職員ID B": "S01"}, "同じ職員"),
    ({"職員ID A": ""}, "未登録"),
    ({"条件": "invalid"}, "条件・適用範囲"),
    ({"適用範囲": "invalid"}, "条件・適用範囲"),
    ({"優先度": "1.5"}, "低め・普通・高め・最優先"),
    ({"優先度": "最重要"}, "低め・普通・高め・最優先"),
])
def test_invalid_pair_settings_are_shown_as_input_errors(changes, message):
    at = generate(inject(small_app(), [pair(**changes)]))
    displayed = " / ".join(e.value for e in [*at.error, *at.markdown])
    assert message in displayed
    assert not at.success


def test_editing_pair_hides_old_result_until_regenerated():
    at = small_app()
    force_a(at)
    generate(inject(at, [pair()]))
    assert at.success
    old_fingerprint = at.session_state.schedule[0]
    inject(at, [pair(**{"条件": "ソフト（できるだけ）"})])
    assert any("入力条件が変更" in w.value for w in at.warning)
    assert not at.success
    assert not any(d.proto.label == "同一シフトペア結果 CSV" for d in at.get("download_button"))
    generate(at)
    assert at.success
    assert at.session_state.schedule[0] != old_fingerprint


@pytest.mark.parametrize("kind", ["同一シフトペア", "回避ペア", "個別勤務時刻"])
def test_csv_import_preserves_other_conditions_and_restores_numeric_types(kind):
    at = inject(small_app(), [pair()])
    frames = {
        "同一シフトペア": pd.DataFrame([pair(**{"条件": "ソフト（できるだけ）", "優先度": "高め"})]),
        "回避ペア": pd.DataFrame([{"職員ID A": "S01", "職員ID B": "S03", "優先度": "低め"}]),
        "個別勤務時刻": pd.DataFrame([{"職員ID": "S03", "日付": "", "シフト": "日勤", "開始": "10:00",
                                    "終了": "15:00", "翌日終了": False, "休憩（分）": 30}]),
    }
    content = frames[kind].to_csv(index=False).encode("utf-8-sig")

    def upload(label, *args, **kwargs):
        return BytesIO(content) if label == f"{kind}CSV（UTF-8）" else None

    with patch("streamlit.file_uploader", side_effect=upload):
        at.run()
        next(b for b in at.button if b.label == f"CSVを{kind}に反映").click().run()
    at.run()
    assert not at.exception
    row = at.session_state.same_pair_source.iloc[0]
    assert row["優先度"] == ("高め" if kind == "同一シフトペア" else "普通")
    assert row["職員ID A"] == "S01"
    generate(at)
    assert at.session_state.schedule[1].has_solution


def test_invalid_csv_does_not_replace_existing_pair():
    at = inject(small_app(), [pair()])
    content = pd.DataFrame([pair(**{"職員ID B": "UNKNOWN"})]).to_csv(index=False).encode("utf-8-sig")
    with patch("streamlit.file_uploader", side_effect=lambda label, **kwargs:
               BytesIO(content) if label == "同一シフトペアCSV（UTF-8）" else None):
        at.run()
        next(b for b in at.button if b.label == "CSVを同一シフトペアに反映").click().run()
    assert not at.exception
    assert any("未登録" in error.value for error in at.error)
    assert at.session_state.same_pair_source.iloc[0]["職員ID B"] == "S02"