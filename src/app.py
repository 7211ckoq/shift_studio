"""可変シフト・職員別勤務条件に対応する日本語 Streamlit UI。"""

from datetime import date, time
from dataclasses import replace
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd
import streamlit as st

from optimizer import (DEFAULT_SHIFT_TIMES, AvoidPair, Employee, RoleRequirement, Settings, ShiftTime, SHIFTS,
                       WorkTimeOverride, coverage_segments, employee_shift_time, month_groups,
                       WeeklyAssignment, avoided_shift_assignments, employee_has_role,
                       pair_conflicts, required_working_minutes, solve_schedule)


HOUR_PRESETS = {"未設定": None, "80時間": 80, "120時間": 120, "140時間": 140, "160時間": 160, "168時間": 168, "カスタム": None}
SHIFT_PRESETS = {
    "三交代": (SHIFTS, DEFAULT_SHIFT_TIMES),
    "二交代": (("日勤", "夜勤"), (ShiftTime(480, 1200), ShiftTime(1200, 1920))),
    "日勤のみ": (("日勤",), (ShiftTime(540, 1080, 60),)),
}
ROLE_SCOPES = {"必須なし": "none", "各シフト": "each_shift", "1日のどこか": "each_day"}
ROLE_SCOPE_LABELS = {value: key for key, value in ROLE_SCOPES.items()}


st.set_page_config(page_title="Shift Studio | シフト最適化", page_icon="📅", layout="wide")
st.markdown("""
<style>
.block-container {max-width: 1440px; padding-top: 2.5rem;}
h1 {letter-spacing: -.05em; font-weight: 800 !important;}
[data-testid="stMetric"] {background: white; padding: 1rem 1.3rem; border: 1px solid #E2E8F0; border-radius: 16px;}
.eyebrow {color: #0D9488; font-size: .8rem; font-weight: 800; letter-spacing: .18em;}
.intro {color: #64748B; margin-bottom: 1.5rem;}
[data-testid="stSidebar"] {border-right: 1px solid #E2E8F0;}
</style>
""", unsafe_allow_html=True)


def default_staff() -> pd.DataFrame:
    return pd.DataFrame([
        {"職員ID": f"S{i:02d}", "氏名": f"職員 {i:02d}", "主任以上": i <= 6,
         "役職:主任": i <= 6,
         "固定シフト": "制限なし", "月間実労働時間プリセット": "未設定", "月間実労働時間（時間）": None,
         "月間上限（時間）": None}
        for i in range(1, 16)
    ])


def csv_bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8-sig")


def fingerprint(staff: pd.DataFrame, days_off: pd.DataFrame, settings: Settings,
                *conditions: pd.DataFrame) -> str:
    payload = staff.to_json(date_format="iso") + days_off.to_json(date_format="iso") + repr(settings)
    payload += "".join(frame.to_json(date_format="iso") for frame in conditions)
    return hashlib.sha256(payload.encode()).hexdigest()


def hours_to_minutes(value, label: str) -> int | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    try:
        hours = Decimal(str(value))
        if not hours.is_finite() or not 0 <= hours <= 744:
            raise ValueError
        return int((hours * 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label}は0〜744時間の数値にしてください。") from exc


def boolean_value(value, label: str, default=False) -> bool:
    if pd.isna(value):
        return default
    normalized = str(value).strip().lower()
    if normalized not in ("true", "false", "1", "0"):
        raise ValueError(f"「{label}」は TRUE/FALSE または 1/0 で指定してください。")
    return normalized in ("true", "1")


def parse_employees(staff: pd.DataFrame, days_off: pd.DataFrame, dates: tuple[date, ...],
                    work_times: pd.DataFrame | None = None,
                    role_names: tuple[str, ...] | list[str] | None = None) -> list[Employee]:
    if staff.empty:
        raise ValueError("職員を1人以上登録してください。")
    if {"月間目標プリセット", "月間目標（時間）", "目標厳守"}.intersection(staff.columns):
        raise ValueError("旧形式の月間目標・目標厳守の列は読み込めません。新形式の職員CSVを保存し直し、月間実労働時間を設定してください。指定時間は必須条件となります。")
    required = {"職員ID", "氏名", "主任以上"}
    if not required.issubset(staff.columns):
        raise ValueError("職員CSVには「職員ID」「氏名」「主任以上」の列が必要です。")
    records = []
    for _, row in staff.iterrows():
        if pd.isna(row["職員ID"]) or pd.isna(row["氏名"]):
            raise ValueError("職員IDと氏名を入力してください。")
        leader = boolean_value(row["主任以上"], "主任以上")
        fixed = row.get("固定シフト", "制限なし")
        fixed = "制限なし" if pd.isna(fixed) or not str(fixed).strip() else str(fixed).strip()
        allowed = None if fixed == "制限なし" else frozenset({fixed})
        preset = row.get("月間実労働時間プリセット", "未設定")
        preset = "未設定" if pd.isna(preset) else str(preset).strip()
        if preset not in HOUR_PRESETS:
            raise ValueError(f"月間実労働時間プリセット「{preset}」は不正です。")
        working = hours_to_minutes(row.get("月間実労働時間（時間）"), "月間実労働時間") if preset == "カスタム" else hours_to_minutes(HOUR_PRESETS[preset], "月間実労働時間")
        if preset == "カスタム" and working is None:
            raise ValueError("カスタムを選択した職員には月間実労働時間（時間）を入力してください。")
        maximum = hours_to_minutes(row.get("月間上限（時間）"), "月間上限")
        names = tuple(role_names or ())
        role_columns = {name: f"役職:{name}" for name in names}
        explicit_roles = any(column in staff.columns for column in role_columns.values())
        role_values = {}
        if explicit_roles:
            for name, column in role_columns.items():
                if column in staff.columns:
                    role_values[name] = boolean_value(row[column], column)
        roles = frozenset(name for name, member in role_values.items() if member) if explicit_roles else None
        records.append((str(row["職員ID"]).strip(), str(row["氏名"]).strip(), leader, allowed, working, maximum, roles))
    ids = {record[0] for record in records}
    off_by_person: dict[str, set[date]] = {pid: set() for pid in ids}
    for _, row in days_off.iterrows():
        pid, raw_date = row.get("職員ID"), row.get("日付")
        id_blank = pd.isna(pid) or not str(pid).strip()
        date_blank = pd.isna(raw_date) or not str(raw_date).strip()
        if id_blank and date_blank:
            continue
        if id_blank or date_blank:
            raise ValueError("休み希望には職員IDと日付の両方を入力してください。")
        pid = str(pid).strip()
        if pid not in ids:
            raise ValueError(f"休み希望の職員ID「{pid}」は職員一覧にありません。")
        try:
            off_date = date.fromisoformat(str(raw_date).strip())
        except ValueError as exc:
            raise ValueError("休み希望の日付は YYYY-MM-DD 形式で指定してください。") from exc
        if off_date not in dates:
            raise ValueError(f"休み希望の {off_date} は作成期間外です。期間または希望日を修正してください。")
        off_by_person[pid].add(off_date)

    employees = [
        Employee(pid, name, leader, frozenset(off_by_person[pid]), allowed, working, maximum, roles=roles)
        for pid, name, leader, allowed, working, maximum, roles in records
    ]
    if work_times is not None:
        overrides: dict[str, list[WorkTimeOverride]] = {pid: [] for pid in ids}
        columns = ["職員ID", "日付", "シフト", "開始", "終了", "翌日終了", "休憩（分）"]
        for row in condition_rows(work_times, columns):
            pid = row["職員ID"]
            if pid not in ids:
                raise ValueError(f"個別勤務時刻の職員ID「{pid}」は職員一覧にありません。")
            try:
                day = date.fromisoformat(row["日付"]) if row["日付"] else None
                begin, end = parse_clock(row["開始"]), parse_clock(row["終了"])
                pause = Decimal(row["休憩（分）"] or "0")
                if not pause.is_finite() or pause != pause.to_integral_value() or not 0 <= pause <= 1439:
                    raise ValueError
                next_day = boolean_value(row["翌日終了"] or "false", "翌日終了")
            except (ValueError, InvalidOperation) as exc:
                raise ValueError("個別勤務時刻は日付 YYYY-MM-DD（空欄は毎回）、時刻 HH:MM、翌日終了 TRUE/FALSE、休憩は0〜1439の整数分で入力してください。") from exc
            if day is not None and day not in dates:
                raise ValueError("個別勤務時刻の日付は作成期間外です。")
            overrides[pid].append(WorkTimeOverride(row["シフト"], ShiftTime(begin, end + 1440 * next_day, int(pause)), day))
        employees = [replace(person, work_time_overrides=tuple(overrides[person.employee_id])) for person in employees]
    return employees


def parse_clock(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isascii() or not part.isdigit() for part in parts):
        raise ValueError("時刻はHH:MM形式にしてください。")
    hour, minute = map(int, parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("時刻の範囲が不正です。")
    return hour * 60 + minute


def condition_rows(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, str]]:
    if not set(columns).issubset(frame.columns):
        raise ValueError(f"CSVには「{'、'.join(columns)}」の列が必要です。")
    rows = []
    for _, row in frame.iterrows():
        normalized = {column: "" if pd.isna(row[column]) else str(row[column]).strip() for column in columns}
        if any(normalized.values()):
            rows.append(normalized)
    return rows


def parse_pairs(frame: pd.DataFrame) -> tuple[AvoidPair, ...]:
    pairs = []
    for row in condition_rows(frame, ["職員ID A", "職員ID B", "重み"]):
        try:
            weight = Decimal(row["重み"] or "1")
            if not weight.is_finite() or weight != weight.to_integral_value() or not 1 <= weight <= 100:
                raise ValueError
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("回避ペアの重みは1〜100の整数で入力してください。") from exc
        pairs.append(AvoidPair(row["職員ID A"], row["職員ID B"], int(weight)))
    return tuple(pairs)


def parse_role_requirements(frame: pd.DataFrame) -> tuple[RoleRequirement, ...]:
    required = ["役職名", "配置条件", "必要人数"]
    if not set(required).issubset(frame.columns):
        raise ValueError("役職設定には「役職名」「配置条件」「必要人数」の列が必要です。")
    requirements = []
    seen = set()
    for _, row in frame.iterrows():
        raw_name = row["役職名"]
        name = "" if pd.isna(raw_name) else str(raw_name)
        raw_scope = row["配置条件"]
        scope_label = "必須なし" if pd.isna(raw_scope) or not str(raw_scope).strip() else str(raw_scope).strip()
        raw_minimum = row["必要人数"]
        if not name.strip() and (pd.isna(raw_minimum) or str(raw_minimum).strip() == ""):
            continue
        if not name.strip() or name != name.strip() or name in ("休", "希望休", "制限なし"):
            raise ValueError("役職名は空欄・前後の空白・予約語を除く文字列で指定してください。")
        if name in seen:
            raise ValueError(f"役職名「{name}」が重複しています。")
        seen.add(name)
        if scope_label not in ROLE_SCOPES:
            raise ValueError(f"役職「{name}」の配置条件を選択してください。")
        try:
            numeric = Decimal(str(raw_minimum))
            if not numeric.is_finite() or numeric != numeric.to_integral_value() or not 1 <= numeric <= 100:
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"役職「{name}」の必要人数は1〜100の整数で指定してください。") from exc
        requirements.append(RoleRequirement(name, ROLE_SCOPES[scope_label], int(numeric)))
    return tuple(requirements)


WEEKDAYS = ("月", "火", "水", "木", "金", "土", "日")


def apply_weekly_preferences(employees: list[Employee], weekly: pd.DataFrame,
                             preferences: pd.DataFrame) -> list[Employee]:
    ids = {person.employee_id for person in employees}
    fixed: dict[str, list[WeeklyAssignment]] = {pid: [] for pid in ids}
    for row in condition_rows(weekly, ["職員ID", "曜日", "シフト"]):
        if row["職員ID"] not in ids or row["曜日"] not in WEEKDAYS or not row["シフト"]:
            raise ValueError("曜日固定には登録済み職員ID・曜日（月〜日）・シフトを入力してください。")
        fixed[row["職員ID"]].append(WeeklyAssignment(WEEKDAYS.index(row["曜日"]), row["シフト"]))
    wishes: dict[str, tuple[frozenset[str], int]] = {}
    for row in condition_rows(preferences, ["職員ID", "回避シフト", "重み"]):
        pid = row["職員ID"]
        if pid not in ids or pid in wishes:
            raise ValueError("シフト回避の職員IDが未登録、または重複しています。")
        try:
            shifts = json.loads(row["回避シフト"] or "[]")
            weight = Decimal(row["重み"] or "1")
            if (not isinstance(shifts, list) or any(not isinstance(s, str) for s in shifts)
                    or len(shifts) != len(set(shifts)) or not weight.is_finite()
                    or weight != weight.to_integral_value() or not 1 <= weight <= 100):
                raise ValueError
        except (ValueError, InvalidOperation) as exc:
            raise ValueError('回避シフトはJSON配列（例: ["深夜", "準夜"]）、重みは1〜100の整数で指定してください。') from exc
        wishes[pid] = (frozenset(shifts), int(weight))
    return [replace(person, weekly_assignments=tuple(fixed[person.employee_id]),
                    avoided_shifts=wishes.get(person.employee_id, (frozenset(), 1))[0],
                    shift_avoidance_weight=wishes.get(person.employee_id, (frozenset(), 1))[1])
            for person in employees]


with st.sidebar:
    if "role_revision" not in st.session_state:
        st.session_state.role_revision = 0
    if "role_source" not in st.session_state:
        st.session_state.role_source = pd.DataFrame([
            {"役職名": "主任", "配置条件": "各シフト", "必要人数": 1}
        ])
    st.markdown("### 📅 Shift Studio")
    st.caption("勤務形態に合わせたシフト最適化")
    st.divider()
    st.markdown("#### 作成条件")
    start = st.date_input("開始日", value=date.today().replace(day=1), format="YYYY/MM/DD")
    days = int(st.number_input("作成日数", min_value=1, max_value=62, value=14))
    minimum = int(st.number_input("各シフトの最低人数 N", min_value=2, max_value=100, value=2))
    with st.expander("役職・配置要件", expanded=True):
        st.caption("役職名・各役職の所属と必要人数を設定します。職員表の「役職:名前」列が所属の正本です。「主任以上」は旧CSV互換用です。")
        previous_roles = st.session_state.role_source.copy()
        role_frame = st.data_editor(
            previous_roles, num_rows="dynamic", hide_index=True, width="stretch",
            key=f"role_editor_{st.session_state.role_revision}",
            column_config={
                "役職名": st.column_config.TextColumn(required=True),
                "配置条件": st.column_config.SelectboxColumn(options=list(ROLE_SCOPES), default="必須なし", required=True),
                "必要人数": st.column_config.NumberColumn(min_value=1, max_value=100, step=1, default=1, required=True),
            },
        )
        try:
            role_requirements = parse_role_requirements(role_frame)
            role_error = None
        except ValueError as exc:
            # 入力途中の不正値で職員の役職列を消さない。最後に有効だった定義を表示用に保持し、生成時はエラーで止める。
            role_requirements = tuple(st.session_state.get("role_requirements", ()))
            role_error = str(exc)
        if role_error:
            st.error(role_error)
        old_names = [str(value) for value in previous_roles.get("役職名", []) if pd.notna(value) and str(value).strip()]
        new_names = [item.name for item in role_requirements]
        removed, added = set(old_names) - set(new_names), set(new_names) - set(old_names)
        if len(removed) == len(added) == 1:
            old_name, new_name = next(iter(removed)), next(iter(added))
            source = st.session_state.get("staff_source")
            if source is not None and f"役職:{old_name}" in source.columns:
                source = source.copy()
                source[f"役職:{new_name}"] = source[f"役職:{old_name}"]
                source.drop(columns=[f"役職:{old_name}"], inplace=True)
                st.session_state.staff_source = source
        if old_names != new_names:
            st.session_state.role_revision += 1
        st.session_state.role_source = role_frame.copy()
        st.session_state.role_requirements = role_requirements
        st.session_state.role_error = role_error
    scope_to_legacy = {"each_shift": "各シフトに1人以上", "each_day": "1日のどこかに1人以上"}
    chief_requirement = next((req for req in role_requirements if req.name == "主任"), None)
    initial_mode = scope_to_legacy.get(chief_requirement.required_on if chief_requirement else "each_shift", "各シフトに1人以上")
    old_chief = previous_roles.loc[previous_roles["役職名"] == "主任", "配置条件"] if "役職名" in previous_roles else pd.Series(dtype=str)
    new_chief = role_frame.loc[role_frame["役職名"] == "主任", "配置条件"] if "役職名" in role_frame else pd.Series(dtype=str)
    if len(old_chief) and len(new_chief) and old_chief.iloc[0] != new_chief.iloc[0]:
        synchronized_mode = scope_to_legacy.get(
            ROLE_SCOPES.get(str(new_chief.iloc[0]), "each_shift"), "各シフトに1人以上"
        )
        st.session_state.leader_mode_widget = synchronized_mode
        st.session_state.leader_mode_value = synchronized_mode
    previous_mode = st.session_state.get("leader_mode_value", initial_mode)
    mode_label = st.radio("主任配置（旧操作との互換）", ["各シフトに1人以上", "1日のどこかに1人以上"],
                          index=0 if initial_mode == "各シフトに1人以上" else 1, key="leader_mode_widget")
    if mode_label != previous_mode and chief_requirement is not None:
        # 旧画面からのラジオ操作も、役職テーブルの主任要件へ反映する。
        updated = role_frame.copy()
        updated.loc[updated["役職名"] == "主任", "配置条件"] = (
            "各シフト" if mode_label == "各シフトに1人以上" else "1日のどこか"
        )
        role_frame = updated
        role_requirements = parse_role_requirements(role_frame)
        st.session_state.role_source = role_frame.copy()
        st.session_state.role_requirements = role_requirements
        st.session_state.role_revision += 1
    st.session_state.leader_mode_value = mode_label
    pattern = st.selectbox("勤務形態", [*SHIFT_PRESETS, "カスタム"], key="shift_pattern")
    if pattern == "カスタム":
        shift_count = int(st.number_input("シフト数", min_value=1, max_value=8, value=3, key="shift_count"))
        shift_names = tuple(st.text_input(f"シフト{i + 1}の名称", value=SHIFTS[i] if i < 3 else f"勤務{i + 1}", key=f"custom_name_{i}").strip() for i in range(shift_count))
        defaults = tuple(DEFAULT_SHIFT_TIMES[i] if i < 3 else ShiftTime(540, 1080, 60) for i in range(shift_count))
    else:
        shift_names, defaults = SHIFT_PRESETS[pattern]
    shift_times = []
    with st.expander("シフト時間の詳細設定", expanded=False):
        st.caption("各勤務の開始日をシフト表の日付とします。日付をまたぐ場合は「翌日終了」を選択してください。")
        for index, (shift, default) in enumerate(zip(shift_names, defaults)):
            # 初期三交代のキーを保ち、他形態は独立した編集状態を持つ。
            widget_id = shift if pattern == "三交代" else f"{pattern}_{index}"
            st.markdown(f"**{shift}**")
            left, right = st.columns(2)
            begin = left.time_input(f"{shift} 開始", value=time(default.start_minute // 60, default.start_minute % 60), step=60, key=f"start_{widget_id}")
            finish = right.time_input(f"{shift} 終了", value=time(default.end_minute % 1440 // 60, default.end_minute % 60), step=60, key=f"end_{widget_id}")
            next_day = st.checkbox(f"{shift} 翌日終了", value=default.end_minute >= 1440, key=f"next_day_{widget_id}")
            pause = int(st.number_input(f"{shift} 休憩（分）", min_value=0, max_value=1439, value=default.break_minutes, step=1, key=f"break_{widget_id}"))
            timing = ShiftTime(begin.hour * 60 + begin.minute, finish.hour * 60 + finish.minute + (1440 if next_day else 0), pause)
            shift_times.append(timing)
            if 0 < timing.duration_minutes <= 1440 and timing.working_minutes > 0:
                st.caption(f"{timing.label} / 実労働{timing.working_minutes // 60}時間{timing.working_minutes % 60}分（休憩{pause}分）")
            else:
                st.error(f"{shift}: 勤務長は0分超〜24時間以内、休憩は勤務長未満にしてください。")
    with st.expander("追加の勤務ルール", expanded=True):
        consecutive = int(st.number_input("連続勤務の上限（日）", min_value=1, max_value=62, value=5))
        rest = int(st.number_input("勤務間の最低休息（時間）", min_value=0, max_value=24, value=11))
        nondecreasing_start_times = st.checkbox("前日より早い出勤を禁止する", value=False, key="nondecreasing_start_times")
        st.caption("有効時は、前日も出勤している職員の開始時刻を前日と同じか遅くします（例：09:00→08:00は禁止、09:00→09:00／10:00は可）。個別時刻を反映し、休日を挟めば比較をリセットします。勤務開始日の時刻で比較し、最低休息は別に守ります。対象期間より前の勤務は比較しません。")
        seconds = int(st.number_input("計算時間の上限（秒）", min_value=1, max_value=120, value=20))
    st.caption(" / ".join(f"{shift} {timing.label}" for shift, timing in zip(shift_names, shift_times)))

legacy_mode = "each_shift" if mode_label == "各シフトに1人以上" else "each_day"
settings = Settings(start, days, minimum, legacy_mode, consecutive, rest, seconds,
                   tuple(shift_times), shift_names, role_requirements=role_requirements,
                   nondecreasing_start_times=nondecreasing_start_times)
st.markdown('<div class="eyebrow">WORKFORCE PLANNING</div>', unsafe_allow_html=True)
st.title("無理のないシフトを、一緒に。")
st.markdown('<div class="intro">勤務形態と職員ごとの条件に合わせて、休み希望を守るシフトを作成します。</div>', unsafe_allow_html=True)
c1, c2, c3 = st.columns(3)
c1.metric("対象期間", f"{days} 日間", f"{start:%m/%d} → {settings.dates[-1]:%m/%d}", delta_color="off")
c2.metric("1日の必要配置", f"{minimum * len(shift_names)} 人以上", f"{len(shift_names)} シフト × {minimum} 人", delta_color="off")
c3.metric("役職配置要件", f"{sum(req.required_on != 'none' for req in role_requirements)} 種類",
          " / ".join(f"{req.name} {ROLE_SCOPE_LABELS[req.required_on]}×{req.minimum}" for req in role_requirements if req.required_on != "none") or "必須なし")
st.write("")

if "staff_source" not in st.session_state:
    st.session_state.staff_source = default_staff()
    st.session_state.off_source = pd.DataFrame({"職員ID": pd.Series(dtype="str"), "日付": pd.Series(dtype="str")})
    st.session_state.editor_revision = 0

if "staff_import_revision" not in st.session_state:
    st.session_state.staff_import_revision = 0

# 旧形式の職員CSVや前バージョンの画面状態には任意列の初期値を補う。
for column in default_staff().columns:
    if column.startswith("役職:"):
        continue
    if column not in st.session_state.staff_source.columns:
        st.session_state.staff_source[column] = default_staff()[column].iloc[0]
current_role_names = [req.name for req in role_requirements]
staff_source = st.session_state.staff_source.copy()
legacy_snapshot = st.session_state.get("role_legacy_snapshot")
if legacy_snapshot is not None and "主任" in current_role_names and "主任以上" in staff_source:
    role_column = "役職:主任"
    prior = legacy_snapshot.set_index("職員ID")
    roles_unchanged = all(
        str(row.get("職員ID", "")) not in prior.index
        or bool(row.get(role_column, False)) == bool(prior.loc[str(row.get("職員ID", "")), role_column])
        for _, row in staff_source.iterrows()
    )
    if roles_unchanged:
        for index, row in staff_source.iterrows():
            pid = str(row.get("職員ID", ""))
            if pid in prior.index and bool(row["主任以上"]) != bool(prior.loc[pid, "主任以上"]):
                staff_source.at[index, role_column] = bool(row["主任以上"])
stale_role_columns = [column for column in staff_source.columns
                      if column.startswith("役職:") and column[3:] not in current_role_names]
if stale_role_columns:
    staff_source.drop(columns=stale_role_columns, inplace=True)
for role_name in current_role_names:
    role_column = f"役職:{role_name}"
    if role_column not in staff_source.columns:
        if role_name == "主任" and "主任以上" in staff_source:
            staff_source[role_column] = staff_source["主任以上"].map(
                lambda value: boolean_value(value, "主任以上")
            )
        else:
            staff_source[role_column] = False
st.session_state.staff_source = staff_source

if "pair_source" not in st.session_state:
    st.session_state.pair_source = pd.DataFrame(columns=["職員ID A", "職員ID B", "重み"])
    st.session_state.work_time_source = pd.DataFrame(columns=["職員ID", "日付", "シフト", "開始", "終了", "翌日終了", "休憩（分）"])
    st.session_state.condition_revision = 0

if "weekly_source" not in st.session_state:
    st.session_state.weekly_source = pd.DataFrame(columns=["職員ID", "曜日", "シフト"])
    st.session_state.preference_source = pd.DataFrame(columns=["職員ID", "回避シフト", "重み"])
    st.session_state.weekly_revision = 0

staff_tab, off_tab, personal_tab, weekly_tab, help_tab = st.tabs(["01  職員の登録", "02  休み希望", "03  配慮・個別勤務", "04  曜日固定・シフト回避", "使い方・ルール"])
with staff_tab:
    st.subheader("チームを登録")
    st.caption("サンプルは15人（主任6人）です。役職の所属は「役職:名前」列で設定します。「主任以上」は旧CSV互換用で、役職所属の判定には使いません。")
    st.info("月間実労働時間は休憩控除後の指定時間に完全一致させる必須条件です。未設定なら時間指定なし。固定シフト・月間上限も必ず守ります。")
    st.caption("月間実労働時間プリセットを選ぶか、「カスタム」で月間実労働時間（時間）を入力してください。指定時間は暦月の対象日数で日割り（整数分に四捨五入）、上限は日割りしません。0時間は勤務禁止です。月またぎ勤務は開始月に全時間を計上し、期間外の勤務は含みません。勤務単位と指定時間が合わない場合も作成不可となります。")
    fixed_options = list(dict.fromkeys(["制限なし", *shift_names, *st.session_state.staff_source["固定シフト"].dropna().astype(str).tolist()]))
    staff = st.data_editor(
        st.session_state.staff_source, num_rows="dynamic", hide_index=True, width="stretch",
        key=f"staff_editor_{st.session_state.editor_revision}_{st.session_state.staff_import_revision}_{st.session_state.role_revision}",
        column_config={
            "職員ID": st.column_config.TextColumn(required=True),
            "氏名": st.column_config.TextColumn(required=True),
            "主任以上": st.column_config.CheckboxColumn(default=False, required=True, help="旧CSV互換用。役職要件の判定は「役職:名前」列を使用します。"),
            **{f"役職:{name}": st.column_config.CheckboxColumn(default=False, required=True) for name in current_role_names},
            "固定シフト": st.column_config.SelectboxColumn(options=fixed_options, default="制限なし"),
            "月間実労働時間プリセット": st.column_config.SelectboxColumn(options=list(HOUR_PRESETS), default="未設定"),
            "月間実労働時間（時間）": st.column_config.NumberColumn(min_value=0, max_value=744, step=0.5, help="カスタム選択時のみ使用。休憩控除後の実労働時間。小数は整数分に四捨五入。"),
            "月間上限（時間）": st.column_config.NumberColumn(min_value=0, max_value=744, step=0.5),
        },
    )
    st.session_state.staff_source = staff.copy()
    if {"職員ID", "主任以上", "役職:主任"}.issubset(staff.columns):
        st.session_state.role_legacy_snapshot = staff[["職員ID", "主任以上", "役職:主任"]].copy()
    with st.expander("職員CSVの読み込み・保存"):
        upload = st.file_uploader("職員CSV（UTF-8）", type="csv")
        if st.button("CSVを職員一覧に反映", disabled=upload is None):
            try:
                imported = pd.read_csv(upload, dtype={"職員ID": str, "氏名": str})
                people = parse_employees(imported, pd.DataFrame(), settings.dates, role_names=current_role_names)
                imported_role_names = [name for name in current_role_names if f"役職:{name}" in imported.columns]
                if role_requirements:
                    migrated_people = []
                    for person in people:
                        if imported_role_names:
                            membership = set(person.roles or ())
                            # 欠落した現在役職の列は、旧CSV項目から主任だけを補完する。
                            if "主任" in current_role_names and "主任" not in imported_role_names and person.is_leader:
                                membership.add("主任")
                            person = replace(person, roles=frozenset(membership))
                        else:
                            person = replace(person, roles=frozenset(
                                {"主任"} if person.is_leader and "主任" in current_role_names else set()
                            ))
                        migrated_people.append(person)
                    people = migrated_people
                # コア側の検証も通し、空IDや重複IDを黙って受理しない。
                from optimizer import validate_inputs
                errors = validate_inputs(people, settings)
                if errors:
                    raise ValueError(" / ".join(errors))
                st.session_state.staff_source = pd.DataFrame([
                    {"職員ID": person.employee_id, "氏名": person.name, "主任以上": person.is_leader,
                    **{f"役職:{name}": employee_has_role(person, name) for name in current_role_names},
                     "固定シフト": next(iter(person.allowed_shifts)) if person.allowed_shifts else "制限なし",
                     "月間実労働時間プリセット": str(imported.iloc[i].get("月間実労働時間プリセット", "未設定")) if pd.notna(imported.iloc[i].get("月間実労働時間プリセット")) else "未設定",
                     "月間実労働時間（時間）": person.monthly_working_minutes / 60 if person.monthly_working_minutes is not None else None,
                     "月間上限（時間）": person.max_monthly_minutes / 60 if person.max_monthly_minutes is not None else None}
                    for i, person in enumerate(people)
                ])
                # 休み希望エディターの状態を維持して職員だけ差し替える。
                st.session_state.staff_import_revision += 1
                st.rerun()
            except (ValueError, UnicodeError, pd.errors.ParserError) as exc:
                st.error(f"読み込めませんでした: {exc}")
        st.download_button("職員CSVを保存", csv_bytes(staff), "staff.csv", "text/csv")

with off_tab:
    st.subheader("休みたい日を指定")
    st.caption("1行に1人・1日を登録します。休み希望は優先度ではなく必須条件です。未入力行は無視します。")
    available_ids = [str(pid).strip() for pid in staff["職員ID"].dropna() if str(pid).strip()]
    days_off = st.data_editor(
        st.session_state.off_source, num_rows="dynamic", hide_index=True, width="stretch",
        key=f"off_editor_{st.session_state.editor_revision}",
        column_config={
            "職員ID": st.column_config.SelectboxColumn(options=list(dict.fromkeys(available_ids))),
            "日付": st.column_config.TextColumn(help="例: 2026-11-03（YYYY-MM-DD）"),
        },
    )
    st.download_button("休み希望CSVを保存", csv_bytes(days_off), "days_off.csv", "text/csv")
    off_upload = st.file_uploader("休み希望CSV（職員ID,日付 / UTF-8）", type="csv")
    if st.button("CSVを休み希望に反映", disabled=off_upload is None):
        try:
            imported_off = pd.read_csv(off_upload, dtype=str)
            if not {"職員ID", "日付"}.issubset(imported_off.columns):
                raise ValueError("「職員ID」「日付」の列が必要です。")
            parse_employees(staff, imported_off, settings.dates, role_names=current_role_names)
            st.session_state.off_source = imported_off[["職員ID", "日付"]]
            # 再生成時に職員エディターの最新値を失わない。
            st.session_state.staff_source = staff.copy()
            st.session_state.editor_revision += 1
            st.rerun()
        except (ValueError, UnicodeError, pd.errors.ParserError) as exc:
            st.error(f"読み込めませんでした: {exc}")

with personal_tab:
    st.subheader("同じシフトをできるだけ避けるペア")
    st.info("同日・同じシフト区分への配置を減らすソフト制約です。必須条件と総配置数の抑制を優先し、その次にペア配慮とシフト回避の重み付き合計、最後に勤務回数の偏りを改善します。回避を保証するものではありません。")
    st.caption("職員IDで指定します。同じ組の逆順・重複は登録できません。重み1〜100で、大きいほど回避を重視します（最大200組）。異なるシフト間の時刻の重なりは対象外です。")
    st.info("**重みの目安**：1〜4＝軽め（同じ組の衝突を少し減らす）／5〜19＝中程度（標準的な配慮）／20〜49＝強め（優先して避ける）／50〜100＝最優先で避けたい。重み10の衝突1回は、重み1の衝突10回分として比較します。これは回避率ではなく、複数のペア間での相対的な優先度です。配置数は増やさず、必須条件も変えないため、避けられない場合は衝突が残ります。", icon="💡")
    pairs_frame = st.data_editor(
        st.session_state.pair_source, num_rows="dynamic", hide_index=True, width="stretch",
        key=f"pair_editor_{st.session_state.condition_revision}",
        column_config={
            "職員ID A": st.column_config.SelectboxColumn(options=list(dict.fromkeys(available_ids))),
            "職員ID B": st.column_config.SelectboxColumn(options=list(dict.fromkeys(available_ids))),
            "重み": st.column_config.NumberColumn(min_value=1, max_value=100, step=1),
        },
    )
    st.subheader("早出・遅出・早退・時短の個別勤務時刻")
    st.caption("1行に職員・シフト・時刻を入力します。日付空欄は毎回適用、日付ありはその日だけ上書きします。割当そのものを強制する設定ではありません。開始・終了は HH:MM、翌00:00終了は終了00:00＋翌日終了。休憩空欄は0分です。")
    st.warning("標準シフト時間内の各時間帯で最低人数を確保し、役職要件が「各シフト」の場合は該当する役職者も設定人数以上確保します。短時間勤務者を終日勤務の1人とは数えません。別シフトの職員による穴埋めや休憩中の人数保証は行いません。早出・延長で標準時間外に勤務する時間も労働時間に計上しますが、その時間の必要人数は設定しません。")
    work_times = st.data_editor(
        st.session_state.work_time_source, num_rows="dynamic", hide_index=True, width="stretch",
        key=f"work_time_editor_{st.session_state.condition_revision}",
        column_config={
            "職員ID": st.column_config.SelectboxColumn(options=list(dict.fromkeys(available_ids))),
            "日付": st.column_config.TextColumn(help="YYYY-MM-DD。空欄なら全対象日に適用"),
            "シフト": st.column_config.SelectboxColumn(options=list(shift_names)),
            "開始": st.column_config.TextColumn(help="例: 09:00"),
            "終了": st.column_config.TextColumn(help="例: 15:30"),
            "翌日終了": st.column_config.CheckboxColumn(),
            "休憩（分）": st.column_config.NumberColumn(min_value=0, max_value=1439, step=1),
        },
    )
    for frame, source, label, filename in (
        (pairs_frame, "pair_source", "回避ペア", "avoid_pairs.csv"),
        (work_times, "work_time_source", "個別勤務時刻", "work_times.csv"),
    ):
        with st.expander(f"{label}CSVの読み込み・保存"):
            condition_upload = st.file_uploader(f"{label}CSV（UTF-8）", type="csv", key=f"upload_{source}")
            if st.button(f"CSVを{label}に反映", disabled=condition_upload is None):
                try:
                    imported_condition = pd.read_csv(condition_upload, dtype=str)
                    candidate_pairs = imported_condition if source == "pair_source" else pairs_frame
                    candidate_times = imported_condition if source == "work_time_source" else work_times
                    candidate_settings = replace(settings, avoid_pairs=parse_pairs(candidate_pairs))
                    candidate_people = parse_employees(staff, days_off, settings.dates, candidate_times, current_role_names)
                    from optimizer import validate_inputs
                    errors = validate_inputs(candidate_people, candidate_settings)
                    if errors:
                        raise ValueError(" / ".join(errors))
                    # CSVは文字列で読み込むが、編集表の数値・真偽値列は型を復元する。
                    candidate_pairs = pd.DataFrame([
                        {"職員ID A": pair.employee_id_a, "職員ID B": pair.employee_id_b, "重み": pair.weight}
                        for pair in candidate_settings.avoid_pairs
                    ], columns=["職員ID A", "職員ID B", "重み"])
                    candidate_times = pd.DataFrame([
                        {"職員ID": person.employee_id, "日付": override.day.isoformat() if override.day else "",
                         "シフト": override.shift,
                         "開始": f"{override.timing.start_minute // 60:02d}:{override.timing.start_minute % 60:02d}",
                         "終了": f"{override.timing.end_minute % 1440 // 60:02d}:{override.timing.end_minute % 60:02d}",
                         "翌日終了": override.timing.end_minute >= 1440,
                         "休憩（分）": override.timing.break_minutes}
                        for person in candidate_people for override in person.work_time_overrides
                    ], columns=list(st.session_state.work_time_source.columns))
                    st.session_state.pair_source = candidate_pairs.copy()
                    st.session_state.work_time_source = candidate_times.copy()
                    st.session_state.staff_source = staff.copy()
                    st.session_state.off_source = days_off.copy()
                    st.session_state.condition_revision += 1
                    st.session_state.editor_revision += 1
                    st.rerun()
                except (ValueError, UnicodeError, pd.errors.ParserError) as exc:
                    st.error(f"読み込めませんでした: {exc}")
            st.download_button(f"{label}CSVを保存", csv_bytes(frame), filename, "text/csv")

with weekly_tab:
    st.subheader("毎週の曜日固定勤務（必須）")
    st.info("例：職員S01を毎週月曜の日勤に必ず配置します。勤務開始日の曜日で判定します。希望休・許可シフト・休息・連勤・月間時間などと矛盾する場合、どちらも緩和せず作成不可となります。")
    st.caption("1行に職員・曜日・シフトを指定。同じ職員・曜日は1件のみです。他の曜日の出勤・休日は固定しません。時刻は標準または個別勤務時刻を適用します。")
    weekly_frame = st.data_editor(
        st.session_state.weekly_source, num_rows="dynamic", hide_index=True, width="stretch",
        key=f"weekly_editor_{st.session_state.weekly_revision}",
        column_config={
            "職員ID": st.column_config.SelectboxColumn(options=list(dict.fromkeys(available_ids))),
            "曜日": st.column_config.SelectboxColumn(options=list(WEEKDAYS)),
            "シフト": st.column_config.SelectboxColumn(options=list(shift_names)),
        },
    )
    st.subheader("職員ごとに避けたいシフト（複数選択・ソフト条件）")
    st.caption("必須条件→配置数の抑制→ペア配慮とシフト回避の重み付き合計→勤務回数の偏りの順に優先します。回避を保証しません。曜日固定と同じシフトを選んでも、固定勤務を優先します。")
    st.caption("重みの目安：1〜4＝軽め、5〜19＝中程度、20〜49＝強め、50〜100＝最優先。回避シフトへの配置1回につき加算し、ペア配慮の同じ重み1回と等価です。回避率を表す数値ではありません。")
    preference_rows = []
    source_preferences = st.session_state.preference_source
    # 削除した職員の条件も黙って捨てず、生成時に入力エラーとして知らせる。
    preference_rows.extend(source_preferences.loc[~source_preferences["職員ID"].isin(available_ids)].to_dict("records"))
    for pid in dict.fromkeys(available_ids):
        defaults = source_preferences.loc[source_preferences["職員ID"] == pid]
        selected = json.loads(defaults.iloc[0]["回避シフト"]) if len(defaults) else []
        weight_default = int(defaults.iloc[0]["重み"]) if len(defaults) else 1
        name_rows = staff.loc[staff["職員ID"].astype(str).str.strip() == pid, "氏名"]
        name = str(name_rows.iloc[0]) if len(name_rows) else pid
        with st.expander(f"{name} [{pid}] のシフト回避"):
            choices = st.multiselect(
                f"{pid} 避けたいシフト", options=list(dict.fromkeys([*shift_names, *selected])), default=selected,
                key=f"avoid_shifts_{st.session_state.weekly_revision}_{pid}",
            )
            weight = int(st.number_input(f"{pid} シフト回避の重み", min_value=1, max_value=100,
                                        value=weight_default, step=1,
                                        key=f"avoid_weight_{st.session_state.weekly_revision}_{pid}"))
        if choices:
            preference_rows.append({"職員ID": pid, "回避シフト": json.dumps(choices, ensure_ascii=False), "重み": weight})
    preferences_frame = pd.DataFrame(preference_rows, columns=["職員ID", "回避シフト", "重み"])
    st.session_state.weekly_source = weekly_frame.copy()
    st.session_state.preference_source = preferences_frame.copy()
    for frame, source, label, filename in (
        (weekly_frame, "weekly_source", "曜日固定", "weekly_assignments.csv"),
        (preferences_frame, "preference_source", "シフト回避", "shift_preferences.csv"),
    ):
        with st.expander(f"{label}CSVの読み込み・保存"):
            incoming = st.file_uploader(f"{label}CSV（UTF-8）", type="csv", key=f"upload_{source}")
            if st.button(f"CSVを{label}に反映", disabled=incoming is None):
                try:
                    imported = pd.read_csv(incoming, dtype=str)
                    candidate_weekly = imported if source == "weekly_source" else weekly_frame
                    candidate_preferences = imported if source == "preference_source" else preferences_frame
                    candidate_people = apply_weekly_preferences(
                        parse_employees(staff, days_off, settings.dates, work_times, current_role_names),
                        candidate_weekly, candidate_preferences,
                    )
                    from optimizer import validate_inputs
                    errors = validate_inputs(candidate_people, settings)
                    if errors:
                        raise ValueError(" / ".join(errors))
                    st.session_state.weekly_source = candidate_weekly.copy()
                    st.session_state.preference_source = pd.DataFrame([
                        {"職員ID": p.employee_id, "回避シフト": json.dumps(sorted(p.avoided_shifts), ensure_ascii=False),
                         "重み": p.shift_avoidance_weight} for p in candidate_people if p.avoided_shifts
                    ], columns=["職員ID", "回避シフト", "重み"])
                    st.session_state.staff_source = staff.copy()
                    st.session_state.off_source = days_off.copy()
                    st.session_state.pair_source = pairs_frame.copy()
                    st.session_state.work_time_source = work_times.copy()
                    st.session_state.weekly_revision += 1
                    st.rerun()
                except (ValueError, UnicodeError, pd.errors.ParserError) as exc:
                    st.error(f"読み込めませんでした: {exc}")
            st.download_button(f"{label}CSVを保存", csv_bytes(frame), filename, "text/csv")

with help_tab:
    st.markdown("""
1. 左のサイドバーで期間・必要人数・役職ごとの配置条件を指定します。
2. 職員一覧と休み希望を編集し、「シフトを作成」を押します。
3. 条件を満たす場合のみシフトを表示します。CSVはExcelで開けるUTF-8 BOM形式です。

**必須条件**：1人1日1勤務まで／各シフトN人以上／設定した役職要件／休み希望の遵守。

**追加条件**：連続勤務上限と勤務間の休息は設定可能。初期値は5日と11時間です。
「前日より早い出勤を禁止する」を選ぶと、連続する勤務開始日の開始時刻を前日と同じか遅くします。
個別時刻にも適用します。休日を挟めば比較をリセットし、期間前の勤務は比較しません。最低休息は別に検査します。
「シフト時間の詳細設定」で開始・終了を分単位で指定できます。終了日が翌日の場合は「翌日終了」を選びます。
勤務形態は日勤のみ・二交代・三交代・カスタム（1〜8シフト）から選べます。
労働時間は開始から終了までの経過時間から各シフトの休憩を控除します。拘束時間は1勤務24時間以内です。
休憩の取得時刻や休憩中の代替配置は計画しないため、休憩中を含む常時人数は保証しません。
希望休は暦日全体を休みとし、前日開始の勤務が希望休当日にかかる割当も禁止します（当日0時終了は可）。
人数と主任以上の配置・連勤は勤務開始日とシフト区分を基準とします。時間帯の隙間や重複の有無は自動調整しません。

**職員別条件**：固定シフト・月間実労働時間・月間上限は必須条件です。指定した実労働時間に必ず完全一致させます。
実労働時間の指定は暦月ごとに対象日数で日割りします。上限は日割りせず、作成対象の勤務だけに適用します。
未設定なら時間指定なし、0時間なら勤務禁止です。指定時間と勤務時間の単位が合わない場合は作成不可となります。
月またぎ勤務の全労働時間は勤務開始月に計上します。対象期間外の既存勤務時間は入力できません。

**個別勤務時刻**：「配慮・個別勤務」で職員・シフトごとの早出・遅出・早退・時短を登録できます。
日付空欄は毎回適用、日付指定があればその日だけ優先します。休息・希望休・月間時間も個別時刻で計算します。
標準シフト時間内の各時間帯で必要人数・各シフトの主任配置を検査します（休憩時刻は対象外）。

**曜日固定**：「04 曜日固定・シフト回避」で毎週の曜日・シフトへの出勤を必須にできます。
希望休などと矛盾する場合も解除しません。その他の曜日の出勤・休日を固定する設定ではありません。

**シフト回避**：職員ごとに避けたいシフトを複数選択できます。回避できなくても作成不可にはしません。

**最適化の優先順位**：すべての必須条件を満たす中で、余剰配置→ペア配慮とシフト回避の重み付き合計→勤務回数の偏りの順に抑えます。
休み希望や役職条件によって、全員の回数を同じにできないことがあります。

**対象外**：期間前後の実勤務、法定休日、週単位の労働時間、休憩取得時刻、その他の個人別契約。
「深夜勤務の翌日を必ず休み」にするルールではありません。
法令・就業規則への適合を保証するものではなく、運用前に責任者が確認してください。
入力データは外部APIに送信せず、このアプリのメモリで処理します。画面再読み込みで失われるためCSVを保存してください。
""")

current_fingerprint = fingerprint(staff, days_off, settings, role_frame, pairs_frame, work_times,
                                  weekly_frame, preferences_frame)
st.divider()
if st.button("シフトを作成", type="primary", width="stretch"):
    # 失敗時に古い結果を今回のものと誤認させない。
    st.session_state.pop("schedule", None)
    try:
        if role_error:
            raise ValueError(role_error)
        settings = replace(settings, avoid_pairs=parse_pairs(pairs_frame))
        employees = parse_employees(staff, days_off, settings.dates, work_times, current_role_names)
        employees = apply_weekly_preferences(employees, weekly_frame, preferences_frame)
        with st.spinner("必要人数・役職配置・休み希望を確認して計算しています…"):
            result = solve_schedule(employees, settings)
        st.session_state.schedule = (current_fingerprint, result, employees, settings)
    except ValueError as exc:
        st.error(str(exc))

if "schedule" in st.session_state:
    saved_fingerprint, result, employees, saved_settings = st.session_state.schedule
    if saved_fingerprint != current_fingerprint:
        st.warning("入力条件が変更されました。現在の条件でシフトを再作成してください。以前の結果は非表示にしています。")
    elif not result.has_solution:
        if result.status == "unknown":
            st.warning("時間内に解を発見できませんでした。")
        else:
            st.error("この条件ではシフトを作成できません。" if result.status == "infeasible" else "入力内容を確認してください。")
        for message in result.messages:
            st.write(f"・{message}")
    else:
        st.subheader("作成されたシフト")
        st.caption(" / ".join(f"{shift} {timing.label}（休憩{timing.break_minutes}分）" for shift, timing in zip(saved_settings.shift_names, saved_settings.shift_times)))
        if result.status == "optimal":
            st.success(f"必須条件をすべて満たす最適解が見つかりました。（{result.elapsed_seconds:.2f}秒）")
        else:
            st.success(f"必須条件をすべて満たすシフトが見つかりました。（{result.elapsed_seconds:.2f}秒）")
            st.info("制限時間内の実行可能解です。勤務の偏りについて最適性は未証明です。")
        by_key = {(assignment.employee_id, assignment.day): assignment.shift for assignment in result.assignments}
        conflicts = pair_conflicts(result.assignments, saved_settings)
        avoided_assignments = avoided_shift_assignments(employees, result.assignments)
        if any(person.avoided_shifts for person in employees):
            st.metric("回避希望シフトへの配置", f"{len(avoided_assignments)} 回")
            if avoided_assignments:
                st.warning("必須条件・配置数・他の配慮を考慮した結果、回避希望シフトへの配置が残っています。")
        if saved_settings.avoid_pairs:
            st.metric("配慮ペアの同日・同シフト配置", f"{len(conflicts)} 回",
                      f"重み付き合計 {sum(pair.weight for pair, _, _ in conflicts)}", delta_color="off")
            if conflicts:
                st.warning("必須条件と配置数を優先した結果、回避ペアの同時配置が残っています。ソフト制約のため作成不可にはしません。")
            else:
                st.success("指定された回避ペアは同日・同シフトに配置されていません。")
        detail_rows = []
        for assignment in result.assignments:
            person = next(p for p in employees if p.employee_id == assignment.employee_id)
            timing = employee_shift_time(person, saved_settings, assignment.day, assignment.shift)
            detail_rows.append({"職員ID": person.employee_id, "氏名": person.name, "日付": assignment.day.isoformat(),
                                "シフト": assignment.shift, "勤務時刻": timing.label, "休憩（分）": timing.break_minutes,
                                "実労働時間": timing.working_minutes / 60})
        details = pd.DataFrame(detail_rows)
        weekdays = "月火水木金土日"
        rows = []
        for person in employees:
            memberships = [name for name in current_role_names if employee_has_role(person, name)]
            role_display = " / ".join(memberships) if role_requirements else ("主任以上" if person.is_leader else "一般")
            row = {"職員ID": person.employee_id, "氏名": person.name, "役職": role_display}
            for day in saved_settings.dates:
                shift = by_key.get((person.employee_id, day))
                value = shift or ("希望休" if day in person.days_off else "休")
                if shift is not None:
                    timing = employee_shift_time(person, saved_settings, day, shift)
                    if timing != saved_settings.shift_times[saved_settings.shift_names.index(shift)]:
                        value = f"{shift} {timing.label}（休憩{timing.break_minutes}分）"
                row[f"{day:%m/%d}({weekdays[day.weekday()]})"] = value
            rows.append(row)
        roster = pd.DataFrame(rows)
        colors = {"深夜": "background-color: #EDE9FE; color: #5B21B6", "日勤": "background-color: #FEF3C7; color: #92400E", "準夜": "background-color: #DBEAFE; color: #1D4ED8", "希望休": "background-color: #FCE7F3; color: #9D174D", "休": "color: #94A3B8"}
        st.dataframe(roster.style.map(lambda value: colors.get(str(value), "")), hide_index=True, width="stretch")
        daily_rows = []
        for day in saved_settings.dates:
            for shift in saved_settings.shift_names:
                people = [person for person in employees if by_key.get((person.employee_id, day)) == shift]
                timing = saved_settings.shift_times[saved_settings.shift_names.index(shift)]
                segments = coverage_segments(employees, saved_settings, day, shift)
                assigned_ids = {person.employee_id for person in people}
                leader_ids = {person.employee_id for person in people if person.is_leader}
                day_people = [person for person in employees if any(
                    by_key.get((person.employee_id, day)) == assigned_shift for assigned_shift in saved_settings.shift_names
                )]
                daily_rows.append({"日付": day.isoformat(), "シフト": shift, "勤務時刻": timing.label, "休憩（分）": timing.break_minutes, "勤務時間": timing.working_minutes / 60, "人数": len(people), "主任以上": sum(person.is_leader for person in people),
                                   "時間帯別最少人数": min(len(assigned_ids.intersection(ids)) for _, _, ids in segments),
                                   "時間帯別最少主任以上": min(len(leader_ids.intersection(ids)) for _, _, ids in segments),
                                   **{f"役職:{name}": sum(employee_has_role(person, name) for person in people) for name in current_role_names},
                                   **{f"時間帯別最少役職:{name}": min(
                                       sum(employee_has_role(person, name) and person.employee_id in ids
                                           and person.employee_id in assigned_ids for person in employees)
                                       for _, _, ids in segments
                                   ) for name in current_role_names},
                                   **{f"1日役職人数:{req.name}": sum(employee_has_role(person, req.name) for person in day_people)
                                      for req in saved_settings.role_requirements},
                                   **{f"必要役職人数:{req.name}": req.minimum if req.required_on == "each_shift" else
                                      (req.minimum if req.required_on == "each_day" and shift == saved_settings.shift_names[0] else None)
                                      for req in saved_settings.role_requirements},
                                   "担当者": " / ".join(f"{person.name} [{person.employee_id}] {employee_shift_time(person, saved_settings, day, shift).label}" for person in people)})
        summary_rows = []
        for person in employees:
            counts = {shift: sum(by_key.get((person.employee_id, day)) == shift for day in saved_settings.dates) for shift in saved_settings.shift_names}
            total = sum(counts.values())
            minutes = sum(employee_shift_time(person, saved_settings, day, shift).working_minutes
                          for day in saved_settings.dates
                          if (shift := by_key.get((person.employee_id, day))) is not None)
            summary_rows.append({"職員ID": person.employee_id, "氏名": person.name, **counts, "勤務日数": total, "休日日数（勤務開始日基準）": days - total, "勤務時間": minutes / 60})
        daily = pd.DataFrame(daily_rows)
        summary = pd.DataFrame(summary_rows)
        monthly_rows = []
        for person in employees:
            for month, indices in month_groups(saved_settings).items():
                actual = sum(employee_shift_time(person, saved_settings, saved_settings.dates[d], shift).working_minutes
                             for d in indices
                             for shift in saved_settings.shift_names
                             if by_key.get((person.employee_id, saved_settings.dates[d])) == shift)
                required = required_working_minutes(person, saved_settings, indices)
                monthly_rows.append({"職員ID": person.employee_id, "氏名": person.name, "月": month,
                    "対象日数": len(indices), "実労働時間": actual / 60,
                    "対象期間の指定実労働時間": required / 60 if required is not None else None,
                    "指定との差（時間）": (actual - required) / 60 if required is not None else None,
                    "月間上限（時間）": person.max_monthly_minutes / 60 if person.max_monthly_minutes is not None else None,
                    "固定シフト": " / ".join(sorted(person.allowed_shifts)) if person.allowed_shifts else "制限なし"})
        monthly = pd.DataFrame(monthly_rows)
        coverage_tab, summary_tab, monthly_tab = st.tabs(["日別の配置・主任以上の確認", "職員別の集計", "月別労働時間"])
        with coverage_tab:
            st.dataframe(daily, hide_index=True, width="stretch")
            st.caption("勤務時刻・休憩・勤務時間は標準設定です。担当者の実時刻と時間帯別最少人数を確認してください。個人別の休憩・実労働時間は勤務明細に記載します。")
        with summary_tab:
            st.dataframe(summary, hide_index=True, width="stretch")
        with monthly_tab:
            st.dataframe(monthly, hide_index=True, width="stretch")
            st.caption("指定実労働時間は対象日数で日割りして完全一致、上限は日割りなし。休憩控除後の全労働時間を勤務開始月に計上。対象期間外の勤務は含みません。")
        download_columns = st.columns(4)
        for column, frame, label, filename in zip(download_columns, (roster, daily, summary, monthly), ("シフト表", "日別配置", "職員別集計", "月別労働時間"), ("roster", "coverage", "summary", "monthly")):
            column.download_button(f"{label} CSV", csv_bytes(frame), f"{filename}_{start.isoformat()}.csv", "text/csv", width="stretch")
        with st.expander("個別勤務明細・ペア配慮の詳細"):
            st.dataframe(details, hide_index=True, width="stretch")
            st.download_button("個別勤務明細 CSV", csv_bytes(details), f"details_{start.isoformat()}.csv", "text/csv")
            conflict_frame = pd.DataFrame([
                {"職員ID A": pair.employee_id_a, "職員ID B": pair.employee_id_b, "日付": day.isoformat(), "シフト": shift, "重み": pair.weight}
                for pair, day, shift in conflicts
            ], columns=["職員ID A", "職員ID B", "日付", "シフト", "重み"])
            st.dataframe(conflict_frame, hide_index=True, width="stretch")
            st.download_button("ペア配慮結果 CSV", csv_bytes(conflict_frame), "pair_conflicts.csv", "text/csv")
        with st.expander("曜日固定・シフト回避の結果"):
            fixed_results = pd.DataFrame([
                {"職員ID": person.employee_id, "日付": day.isoformat(), "曜日": WEEKDAYS[day.weekday()],
                 "固定シフト": fixed.shift, "割当シフト": by_key.get((person.employee_id, day), "休")}
                for person in employees for fixed in person.weekly_assignments
                for day in saved_settings.dates if day.weekday() == fixed.weekday
            ], columns=["職員ID", "日付", "曜日", "固定シフト", "割当シフト"])
            preference_results = pd.DataFrame([
                {"職員ID": assignment.employee_id, "日付": assignment.day.isoformat(), "回避シフト": assignment.shift,
                 "重み": next(p.shift_avoidance_weight for p in employees if p.employee_id == assignment.employee_id)}
                for assignment in avoided_assignments
            ], columns=["職員ID", "日付", "回避シフト", "重み"])
            st.dataframe(fixed_results, hide_index=True, width="stretch")
            st.dataframe(preference_results, hide_index=True, width="stretch")
            st.download_button("曜日固定結果 CSV", csv_bytes(fixed_results), "weekly_results.csv", "text/csv")
            st.download_button("シフト回避結果 CSV", csv_bytes(preference_results), "preference_results.csv", "text/csv")
        st.download_button("作成条件を保存（JSON）", json.dumps({
            "開始日": start.isoformat(), "日数": days, "最低人数": minimum,
            "主任以上の配置": settings.leader_mode, "連続勤務上限": consecutive,
            "役職要件": [{"役職名": req.name, "配置条件": req.required_on, "必要人数": req.minimum} for req in saved_settings.role_requirements],
            "最低休息時間": rest, "計算時間上限": seconds,
            "前日より早い出勤を禁止": saved_settings.nondecreasing_start_times,
            "勤務形態": pattern,
            "回避ペア": [{"職員ID A": pair.employee_id_a, "職員ID B": pair.employee_id_b, "重み": pair.weight} for pair in saved_settings.avoid_pairs],
            "個別勤務時刻": work_times.where(pd.notna(work_times), None).to_dict(orient="records"),
            "曜日固定": weekly_frame.where(pd.notna(weekly_frame), None).to_dict(orient="records"),
            "シフト回避": preferences_frame.to_dict(orient="records"),
            "シフト時刻": {shift: {"開始分": timing.start_minute, "終了分（開始日0時基準）": timing.end_minute, "休憩分": timing.break_minutes, "実労働分": timing.working_minutes, "表示": timing.label} for shift, timing in zip(saved_settings.shift_names, saved_settings.shift_times)},
            "月間集計ルール": "開始月に全時間を計上。指定実労働時間は対象日数で日割り・整数分に四捨五入して完全一致。上限は日割りなし。対象期間外の勤務は含まない。",
            "職員別条件は職員CSVを参照": True,
        }, ensure_ascii=False, indent=2), "settings.json", "application/json")
        st.caption("必須条件の再検査済み。対象期間の前後の勤務や、就業規則・法令上の条件は別途確認してください。")