"""名前付き役職のCSV互換・入力画面・結果表示を検証する。"""

from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from app_helpers import all_workdays

from app import parse_employees, parse_role_requirements
from optimizer import RoleRequirement


START = date(2026, 1, 1)


def test_parse_role_requirements_and_validate_duplicate_names():
    frame = pd.DataFrame([
        {"役職名": "看護", "配置条件": "各シフト", "必要人数": 2},
        {"役職名": "主任", "配置条件": "1日のどこか", "必要人数": 1},
    ])
    assert parse_role_requirements(frame) == (
        RoleRequirement("看護", "each_shift", 2),
        RoleRequirement("主任", "each_day", 1),
    )
    with pytest.raises(ValueError, match="重複"):
        parse_role_requirements(pd.concat([frame.iloc[[0]], frame.iloc[[0]]], ignore_index=True))


def test_parse_employees_uses_explicit_roles_or_legacy_none():
    days = (START,)
    legacy = pd.DataFrame([
        {"職員ID": "A", "氏名": "旧主任", "主任以上": True},
        {"職員ID": "B", "氏名": "一般", "主任以上": False},
    ])
    old_people = parse_employees(legacy, pd.DataFrame(), days, role_names=("主任", "看護"))
    assert [person.roles for person in old_people] == [None, None]

    explicit = legacy.assign(**{"役職:主任": [False, True], "役職:看護": [True, False]})
    people = parse_employees(explicit, pd.DataFrame(), days, role_names=("主任", "看護"))
    assert people[0].roles == frozenset({"看護"})
    assert people[1].roles == frozenset({"主任"})

    explicit.loc[0, "役職:看護"] = "perhaps"
    with pytest.raises(ValueError, match="TRUE/FALSE"):
        parse_employees(explicit, pd.DataFrame(), days, role_names=("主任", "看護"))


def test_role_columns_are_required_boolean_values_when_present():
    frame = pd.DataFrame([{
        "職員ID": "A", "氏名": "看護師", "主任以上": False, "役職:看護": "yes",
    }])
    with pytest.raises(ValueError, match="TRUE/FALSE"):
        parse_employees(frame, pd.DataFrame(), (START,), role_names=("主任", "看護"))


def test_role_requirements_generate_and_appear_in_results():
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()
    assert not at.exception
    at.date_input[0].set_value((START, START + timedelta(days=1 - 1)))
    next(item for item in at.number_input if item.label == "計算時間の上限（秒）").set_value(5)
    at.selectbox(key="shift_pattern").select("日勤のみ")

    source = at.session_state.staff_source.iloc[:2].copy()
    source["主任以上"] = [False, False]
    source["役職:主任"] = [True, False]
    source["役職:看護"] = [True, True]
    at.session_state.staff_source = source
    at.session_state.role_source = pd.DataFrame([
        {"役職名": "主任", "配置条件": "各シフト", "必要人数": 1},
        {"役職名": "看護", "配置条件": "1日のどこか", "必要人数": 2},
    ])
    at.session_state.role_revision += 1
    at.session_state.editor_revision += 1
    at.run()
    assert not at.exception

    next(button for button in at.button if button.label == "シフトを作成").click().run()
    assert not at.exception
    _, result, people, settings = at.session_state.schedule
    assert result.has_solution, result.messages
    assert settings.role_requirements == (
        RoleRequirement("主任", "each_shift", 1),
        RoleRequirement("看護", "each_day", 2),
    )
    assert all(person.roles is not None for person in people)

    roster = next(frame.value for frame in at.dataframe if "役職" in frame.value.columns and "01/01(木)" in frame.value.columns)
    assert roster.set_index("職員ID").loc["S01", "役職"] == "主任 / 看護"
    daily = next(frame.value for frame in at.dataframe if "必要役職人数:看護" in frame.value.columns)
    assert daily.iloc[0]["役職:看護"] == 2
    assert daily.iloc[0]["1日役職人数:看護"] == 2
    assert daily.iloc[0]["必要役職人数:看護"] == 2
    settings_json = next(button for button in at.get("download_button") if button.proto.label == "作成条件を保存（JSON）")
    assert settings_json.proto.url


def test_old_staff_csv_import_migrates_leader_flag_to_chief_membership():
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()
    assert not at.exception
    old_csv = at.session_state.staff_source.drop(columns=["役職:主任"]).copy()
    content = old_csv.to_csv(index=False).encode("utf-8-sig")

    def upload(label, *_args, **_kwargs):
        del _args, _kwargs
        return BytesIO(content) if label == "職員CSV（UTF-8）" else None

    with patch("streamlit.file_uploader", side_effect=upload):
        at.run()
        next(button for button in at.button if button.label == "CSVを職員一覧に反映").click().run()
    assert not at.exception
    staff = at.session_state.staff_source
    assert staff["役職:主任"].tolist() == staff["主任以上"].tolist()
    assert "役職:看護" not in staff


def test_invalid_role_definition_does_not_drop_existing_membership_columns():
    at = all_workdays(AppTest.from_file(str(Path(__file__).resolve().parents[1] / "src" / "app.py"), default_timeout=45)).run()
    assert not at.exception
    original = at.session_state.staff_source["役職:主任"].tolist()
    at.session_state.role_source = pd.DataFrame([
        {"役職名": "", "配置条件": "各シフト", "必要人数": 1}
    ])
    at.session_state.role_revision += 1
    at.run()
    assert not at.exception
    assert "役職:主任" in at.session_state.staff_source
    assert at.session_state.staff_source["役職:主任"].tolist() == original
    assert any("役職名" in error.value for error in at.error)
