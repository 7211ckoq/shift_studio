"""三交代勤務のハード制約を守る CP-SAT モデル。UI から独立して利用できる。"""

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from ortools.sat.python import cp_model


SHIFTS = ("深夜", "日勤", "準夜")
LeaderMode = Literal["each_shift", "each_day"]
RoleCoverage = Literal["none", "each_shift", "each_day"]


@dataclass(frozen=True)
class RoleRequirement:
    name: str
    required_on: RoleCoverage = "none"
    minimum: int = 1


@dataclass(frozen=True)
class ShiftTime:
    """開始日0時からの分数。終了は翌日も指定でき、勤務長は24時間以内。"""
    start_minute: int
    end_minute: int
    break_minutes: int = 0

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute

    @property
    def working_minutes(self) -> int:
        return self.duration_minutes - self.break_minutes

    @property
    def label(self) -> str:
        start = f"{self.start_minute // 60:02d}:{self.start_minute % 60:02d}"
        end = f"{self.end_minute % 1440 // 60:02d}:{self.end_minute % 60:02d}"
        return f"{start}–{'翌日 ' if self.end_minute >= 1440 else ''}{end}"


DEFAULT_SHIFT_TIMES = (ShiftTime(0, 480), ShiftTime(480, 960), ShiftTime(960, 1440))


@dataclass(frozen=True)
class WorkTimeOverride:
    shift: str
    timing: ShiftTime
    day: date | None = None


@dataclass(frozen=True)
class WeeklyAssignment:
    """勤務開始日の曜日固定。月曜0〜日曜6。"""
    weekday: int
    shift: str


@dataclass(frozen=True)
class Employee:
    employee_id: str
    name: str
    is_leader: bool = False
    days_off: frozenset[date] = field(default_factory=frozenset)
    allowed_shifts: frozenset[str] | None = None
    monthly_working_minutes: int | None = None
    max_monthly_minutes: int | None = None
    roles: frozenset[str] | None = field(default=None, kw_only=True)
    weekly_assignments: tuple[WeeklyAssignment, ...] = field(default=(), kw_only=True)
    avoided_shifts: frozenset[str] = field(default_factory=frozenset, kw_only=True)
    shift_avoidance_weight: int = field(default=1, kw_only=True)
    work_time_overrides: tuple[WorkTimeOverride, ...] = ()


@dataclass(frozen=True)
class AvoidPair:
    employee_id_a: str
    employee_id_b: str
    weight: int = 1


@dataclass(frozen=True)
class SameShiftPair:
    """A勤務時にBの同日・同一区分への配置を要求。bothは逆方向にも適用。"""
    employee_id_a: str
    employee_id_b: str
    mode: Literal["hard", "soft"] = "hard"
    direction: Literal["a_to_b", "both"] = "a_to_b"
    weight: int = 1


@dataclass(frozen=True)
class ShiftMerge:
    """指定日だけ複数シフトを1勤務に統合。一覧で最も上のシフト名で記録する。"""
    day: date
    shifts: tuple[str, ...]
    break_minutes: int | None = None  # None なら統合元シフトの休憩の合計


@dataclass(frozen=True)
class StaffingRule:
    """日付・曜日・シフトごとの必要人数の上書き。日付と曜日が両方空なら毎日。
    shift が None なら全シフト。maximum が None なら上限なし。最低＝最大で人数を固定する。"""
    minimum: int
    maximum: int | None = None
    day: date | None = None
    weekday: int | None = None  # 月曜0〜日曜6
    shift: str | None = None


@dataclass(frozen=True)
class Settings:
    start_date: date
    days: int = 14
    minimum_staff: int = 2
    leader_mode: LeaderMode = "each_shift"
    max_consecutive_days: int = 5
    minimum_rest_hours: int = 11
    time_limit_seconds: float = 20
    shift_times: tuple[ShiftTime, ...] = DEFAULT_SHIFT_TIMES
    shift_names: tuple[str, ...] = SHIFTS
    role_requirements: tuple[RoleRequirement, ...] = field(default=(), kw_only=True)
    nondecreasing_start_times: bool = field(default=False, kw_only=True)
    avoid_pairs: tuple[AvoidPair, ...] = ()
    same_shift_pairs: tuple[SameShiftPair, ...] = field(default=(), kw_only=True)
    shift_merges: tuple[ShiftMerge, ...] = field(default=(), kw_only=True)
    # 休日（休業日）。シフトを作らず誰も勤務しない。期間外の日付は無視する。
    closed_days: frozenset[date] = field(default_factory=frozenset, kw_only=True)
    # 基本の最大人数（None なら上限なし）と、日付・曜日・シフトごとの上書き。
    maximum_staff: int | None = field(default=None, kw_only=True)
    staffing_rules: tuple[StaffingRule, ...] = field(default=(), kw_only=True)

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(self.start_date + timedelta(days=i) for i in range(self.days))


def employee_has_role(person: Employee, role: str) -> bool:
    """明示された役割を優先し、未設定時だけ旧主任フラグを互換利用する。"""
    if person.roles is None:
        return role == "主任" and person.is_leader
    return role in person.roles


def _merge_for(settings: Settings, day: date, shift: str) -> ShiftMerge | None:
    return next((merge for merge in settings.shift_merges
                 if merge.day == day and shift in merge.shifts), None)


def merged_shifts(settings: Settings, day: date, shift: str) -> tuple[str, ...]:
    """統合元のシフトを一覧順で返す。統合なしなら自身のみ。"""
    merge = _merge_for(settings, day, shift)
    if merge is None:
        return (shift,)
    return tuple(name for name in settings.shift_names if name in merge.shifts)


def representative_shift(settings: Settings, day: date, shift: str) -> str:
    return merged_shifts(settings, day, shift)[0]


def day_shift_names(settings: Settings, day: date) -> tuple[str, ...]:
    """その日に配置対象となるシフト。休日は空、統合された代表以外のシフトは除く。"""
    if day in settings.closed_days:
        return ()
    return tuple(name for name in settings.shift_names if representative_shift(settings, day, name) == name)


def staff_bounds(settings: Settings, day: date, shift: str) -> tuple[int, int | None]:
    """必要人数（最低, 最大）。日付指定＞曜日指定＞毎日＞基本設定、同じ段ではシフト指定＞全シフト。"""
    def rank(rule: StaffingRule) -> int | None:
        if rule.day is not None:
            level = 0 if rule.day == day else None
        elif rule.weekday is not None:
            level = 2 if rule.weekday == day.weekday() else None
        else:
            level = 4
        if level is None or (rule.shift is not None and rule.shift != shift):
            return None
        return level + (rule.shift is None)

    matches = [(rank(rule), rule) for rule in settings.staffing_rules]
    matches = [(level, rule) for level, rule in matches if level is not None]
    if not matches:
        return settings.minimum_staff, settings.maximum_staff
    rule = min(matches, key=lambda item: item[0])[1]
    return rule.minimum, rule.maximum


def shift_label(settings: Settings, day: date, shift: str) -> str:
    return "+".join(merged_shifts(settings, day, shift))


def _merged_timing(settings: Settings, merge: ShiftMerge) -> ShiftTime | None:
    """統合後の勤務時刻。時間が途切れる組み合わせは None。"""
    timings = sorted((settings.shift_times[settings.shift_names.index(name)] for name in merge.shifts),
                     key=lambda timing: timing.start_minute)
    end = timings[0].end_minute
    for timing in timings[1:]:
        if timing.start_minute > end:
            return None
        end = max(end, timing.end_minute)
    pause = sum(timing.break_minutes for timing in timings) if merge.break_minutes is None else merge.break_minutes
    return ShiftTime(timings[0].start_minute, end, pause)


def shift_base_time(settings: Settings, day: date, shift: str) -> ShiftTime:
    """その日の標準時刻。統合の代表シフトなら統合後の時刻。"""
    merge = _merge_for(settings, day, shift)
    if merge is not None and representative_shift(settings, day, shift) == shift:
        return _merged_timing(settings, merge)
    return settings.shift_times[settings.shift_names.index(shift)]


def employee_shift_time(person: Employee, settings: Settings, day: date, shift: str) -> ShiftTime:
    """日付指定、定例の個別時刻、標準時刻の順で解決する。配置は強制しない。
    統合日は定例の個別時刻を使わず、日付指定がなければ統合後の時刻とする。"""
    base = shift_base_time(settings, day, shift)
    merged = _merge_for(settings, day, shift) is not None
    recurring = None
    for override in person.work_time_overrides:
        if override.shift == shift:
            if override.day == day:
                return override.timing
            if override.day is None and not merged:
                recurring = override.timing
    return recurring if recurring is not None else base


def _allowed(person: Employee, settings: Settings, day: date, shift: str) -> bool:
    """統合日は統合元のどれかが許可シフトなら可。"""
    return person.allowed_shifts is None or any(
        name in person.allowed_shifts for name in merged_shifts(settings, day, shift))


def _eligible(person: Employee, settings: Settings, day: date, shift: str) -> bool:
    if shift not in day_shift_names(settings, day):
        return False
    timing = employee_shift_time(person, settings, day, shift)
    return (
        _allowed(person, settings, day, shift)
        and day not in person.days_off
        and not (timing.end_minute > 1440 and day + timedelta(days=1) in person.days_off)
    )


def coverage_segments(
    employees: list[Employee], settings: Settings, day: date, shift: str,
) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    """元の開始日・勤務区分内で標準時間帯を分割。休憩の時刻は扱わない。"""
    base = shift_base_time(settings, day, shift)
    timings = [(person, employee_shift_time(person, settings, day, shift)) for person in employees]
    boundaries = sorted({base.start_minute, base.end_minute} | {
        minute for _, timing in timings for minute in (timing.start_minute, timing.end_minute)
        if base.start_minute < minute < base.end_minute
    })
    return tuple(
        (start, end, tuple(person.employee_id for person, timing in timings
                           if timing.start_minute <= start and timing.end_minute >= end
                           and _eligible(person, settings, day, shift)))
        for start, end in zip(boundaries, boundaries[1:])
    )


def month_groups(settings: Settings) -> dict[str, tuple[int, ...]]:
    """対象日を勤務開始日の暦月で区分。月またぎ勤務は開始月に全時間を計上。"""
    groups: dict[str, list[int]] = {}
    for d, day in enumerate(settings.dates):
        groups.setdefault(day.strftime("%Y-%m"), []).append(d)
    return {month: tuple(indices) for month, indices in groups.items()}


def required_working_minutes(person: Employee, settings: Settings, indices: tuple[int, ...]) -> int | None:
    """指定した月間実労働時間を対象日数で日割りした、必須の実労働分。"""
    if person.monthly_working_minutes is None:
        return None
    day = settings.dates[indices[0]]
    # 四捨五入（整数分）。指定時間だけを日割りし、上限値は日割りしない。
    denominator = monthrange(day.year, day.month)[1]
    return (person.monthly_working_minutes * len(indices) * 2 + denominator) // (2 * denominator)


@dataclass(frozen=True)
class Assignment:
    employee_id: str
    day: date
    shift: str


def pair_conflicts(
    assignments: tuple[Assignment, ...], settings: Settings,
) -> tuple[tuple[AvoidPair, date, str], ...]:
    """同一開始日・同一区分のペアを列挙。時刻の重なりや重複行は数えない。"""
    groups: dict[tuple[date, str], set[str]] = {}
    for assignment in assignments:
        groups.setdefault((assignment.day, assignment.shift), set()).add(assignment.employee_id)
    return tuple(
        (pair, day, shift)
        for pair in settings.avoid_pairs for day, shift in sorted(groups)
        if pair.employee_id_a in groups[day, shift] and pair.employee_id_b in groups[day, shift]
    )


def avoided_shift_assignments(
    employees: list[Employee], assignments: tuple[Assignment, ...], settings: Settings | None = None,
) -> tuple[Assignment, ...]:
    """回避希望の勤務帯への配置を入力順に列挙。統合日は統合元のどれかが回避対象なら含む。"""
    preferences = {person.employee_id: person.avoided_shifts for person in employees}
    return tuple(
        a for a in assignments
        if preferences.get(a.employee_id, frozenset()).intersection(
            (a.shift,) if settings is None else merged_shifts(settings, a.day, a.shift))
    )


def same_shift_violations(
    assignments: tuple[Assignment, ...], settings: Settings,
) -> tuple[tuple[SameShiftPair, date, str, str], ...]:
    """相手不在の勤務を列挙。双方向の別区分勤務は各人の勤務を1件ずつ数える。"""
    placed = {(a.employee_id, a.day, a.shift) for a in assignments}
    violations = []
    for pair in settings.same_shift_pairs:
        directions = [(pair.employee_id_a, pair.employee_id_b)]
        if pair.direction == "both":
            directions.append((pair.employee_id_b, pair.employee_id_a))
        for source, target in directions:
            for day in settings.dates:
                for shift in settings.shift_names:
                    if (source, day, shift) in placed and (target, day, shift) not in placed:
                        violations.append((pair, day, shift, source))
    return tuple(violations)


@dataclass(frozen=True)
class ScheduleResult:
    status: Literal["optimal", "feasible", "infeasible", "unknown", "invalid"]
    assignments: tuple[Assignment, ...] = ()
    messages: tuple[str, ...] = ()
    elapsed_seconds: float = 0

    @property
    def has_solution(self) -> bool:
        return self.status in ("optimal", "feasible")


def _valid_timing(timing: object) -> bool:
    return (
        isinstance(timing, ShiftTime)
        and type(timing.start_minute) is int
        and type(timing.end_minute) is int
        and 0 <= timing.start_minute < 1440
        and 0 < timing.duration_minutes <= 1440
        and type(timing.break_minutes) is int
        and 0 <= timing.break_minutes < timing.duration_minutes
    )


def validate_inputs(employees: list[Employee], settings: Settings) -> list[str]:
    errors = []
    if type(settings.nondecreasing_start_times) is not bool:
        errors.append("前日より早い出勤の禁止は真偽値で指定してください。")
    if not isinstance(settings.start_date, date):
        errors.append("開始日は日付で指定してください。")
    if not isinstance(settings.days, int) or isinstance(settings.days, bool) or not 1 <= settings.days <= 62:
        errors.append("作成期間は1〜62日の整数で指定してください。")
    if not isinstance(settings.minimum_staff, int) or isinstance(settings.minimum_staff, bool) or settings.minimum_staff < 1:
        errors.append("各シフトの最低人数 N は1以上の整数で指定してください。")
    if settings.maximum_staff is not None and (
            type(settings.maximum_staff) is not int or not isinstance(settings.minimum_staff, int)
            or settings.maximum_staff < settings.minimum_staff):
        errors.append("各シフトの最大人数は最低人数 N 以上の整数で指定してください（上限なしは未設定）。")
    has_role_requirements = bool(settings.role_requirements)
    role_names = []
    if not has_role_requirements and settings.leader_mode not in ("each_shift", "each_day"):
        errors.append("主任以上の配置条件が不正です。")
    if not isinstance(settings.role_requirements, tuple):
        errors.append("役割要件はRoleRequirementのタプルで指定してください。")
    else:
        for requirement in settings.role_requirements:
            if not isinstance(requirement, RoleRequirement):
                errors.append("役割要件はRoleRequirementで指定してください。")
                continue
            name = requirement.name
            if (not isinstance(name, str) or not name.strip() or name != name.strip()
                    or name in ("休", "希望休", "制限なし")):
                errors.append("役割名は空欄・前後の空白・予約語（休、希望休、制限なし）を除く文字列で指定してください。")
            else:
                role_names.append(name)
            if requirement.required_on not in ("none", "each_shift", "each_day"):
                errors.append(f"{name}: 役割の配置条件が不正です。")
            if (type(requirement.minimum) is not int or not 1 <= requirement.minimum
                    or requirement.minimum > min(len(employees), 100)):
                errors.append(f"{name}: 役割の最低人数は1〜職員数（最大100人）の整数で指定してください。")
        if len(set(role_names)) != len(role_names):
            errors.append("役割名が重複しています。")
    if not isinstance(settings.max_consecutive_days, int) or not 1 <= settings.max_consecutive_days <= 62:
        errors.append("連続勤務上限は1〜62日の整数で指定してください。")
    if not isinstance(settings.minimum_rest_hours, int) or not 0 <= settings.minimum_rest_hours <= 24:
        errors.append("勤務間の休息は0〜24時間の整数で指定してください。")
    if not 0 < settings.time_limit_seconds <= 120:
        errors.append("計算時間上限は0より大きく120秒以下にしてください。")
    if (
        not 1 <= len(settings.shift_names) <= 8
        or any(not isinstance(name, str) or not name.strip() or name != name.strip() or name in ("休", "希望休", "制限なし") for name in settings.shift_names)
        or len(set(settings.shift_names)) != len(settings.shift_names)
    ):
        errors.append("シフト名は重複のない1〜8種類で指定してください。空欄・前後の空白・予約語（休、希望休、制限なし）は使えません。")
    if len(settings.shift_times) != len(settings.shift_names):
        errors.append("すべてのシフトに時刻を指定してください。")
    else:
        for name, timing in zip(settings.shift_names, settings.shift_times):
            if not _valid_timing(timing):
                errors.append(f"{name}: 開始時刻は当日、勤務長は0分超〜24時間以内、休憩は0分以上で勤務長未満にしてください。")
    valid_dates = None
    if type(settings.start_date) is date and type(settings.days) is int and 1 <= settings.days <= 62:
        try:
            valid_dates = set(settings.dates)
            # 持越し希望休の検査に必要な翌日も表現できること。
            settings.dates[-1] + timedelta(days=1)
        except OverflowError:
            errors.append("作成期間が日付の範囲を超えています。")
    if not isinstance(settings.closed_days, (set, frozenset)) or any(
            type(day) is not date for day in settings.closed_days):
        errors.append("休日は日付のsetまたはfrozensetで指定してください。")
    errors.extend(_staffing_rule_errors(settings, valid_dates))
    merge_errors = _merge_errors(settings, valid_dates)
    errors.extend(merge_errors)
    merges_valid = not merge_errors
    if not employees:
        errors.append("職員を1人以上登録してください。")
    ids = [person.employee_id for person in employees]
    if any(not person.employee_id.strip() or not person.name.strip() for person in employees):
        errors.append("職員IDと氏名は空にできません。")
    if len(set(ids)) != len(ids):
        errors.append("職員IDが重複しています。氏名が同じ職員にも別のIDを付けてください。")
    for person in employees:
        if not isinstance(person.weekly_assignments, tuple):
            errors.append(f"{person.name}: 曜日固定はWeeklyAssignmentのタプルで指定してください。")
        else:
            seen_weekdays = set()
            for fixed in person.weekly_assignments:
                if not isinstance(fixed, WeeklyAssignment):
                    errors.append(f"{person.name}: 曜日固定はWeeklyAssignmentで指定してください。")
                    continue
                if type(fixed.weekday) is not int or not 0 <= fixed.weekday <= 6:
                    errors.append(f"{person.name}: 曜日固定の曜日は月曜0〜日曜6の整数で指定してください。")
                else:
                    if fixed.weekday in seen_weekdays:
                        errors.append(f"{person.name}: 曜日固定の曜日が重複しています。")
                    seen_weekdays.add(fixed.weekday)
                if not isinstance(fixed.shift, str) or fixed.shift not in settings.shift_names:
                    errors.append(f"{person.name}: 曜日固定のシフトが現在のシフト一覧にありません。")
        if not isinstance(person.avoided_shifts, (set, frozenset)) or any(
            not isinstance(shift, str) or shift not in settings.shift_names
            for shift in person.avoided_shifts
        ):
            errors.append(f"{person.name}: 回避シフトは現在のシフト名のsetまたはfrozensetで指定してください。")
        if type(person.shift_avoidance_weight) is not int or not 1 <= person.shift_avoidance_weight <= 100:
            errors.append(f"{person.name}: シフト回避の重みは1〜100の整数で指定してください。")
        if person.roles is not None:
            if not isinstance(person.roles, (set, frozenset)) or any(
                not isinstance(role, str) for role in person.roles
            ):
                errors.append(f"{person.name}: 役割は文字列のsetまたはfrozensetで指定してください。")
            elif has_role_requirements and any(role not in role_names for role in person.roles):
                errors.append(f"{person.name}: 定義されていない役割が指定されています。")
        if person.allowed_shifts is not None and (not person.allowed_shifts or not person.allowed_shifts.issubset(settings.shift_names)):
            errors.append(f"{person.name}: 固定・許可シフトが現在のシフト一覧にありません。")
        for label, value in (("月間実労働時間", person.monthly_working_minutes), ("月間上限", person.max_monthly_minutes)):
            if value is not None and (type(value) is not int or not 0 <= value <= 744 * 60):
                errors.append(f"{person.name}: {label}は0〜744時間を整数分で指定してください。")
        if (person.monthly_working_minutes is not None and person.max_monthly_minutes is not None
                and person.monthly_working_minutes > person.max_monthly_minutes):
            errors.append(f"{person.name}: 月間実労働時間は月間上限以下にしてください。")
        seen_overrides = set()
        for override in person.work_time_overrides:
            if not isinstance(override, WorkTimeOverride):
                errors.append(f"{person.name}: 個別勤務時刻はWorkTimeOverrideで指定してください。")
                continue
            if not isinstance(override.shift, str) or override.shift not in settings.shift_names:
                errors.append(f"{person.name}: 個別勤務時刻のシフトが現在のシフト一覧にありません。")
            elif person.allowed_shifts is not None and override.shift not in person.allowed_shifts:
                errors.append(f"{person.name}: 個別勤務時刻は許可シフトに指定してください。")
            if not _valid_timing(override.timing):
                errors.append(f"{person.name}: 個別勤務時刻の開始・勤務長・休憩が不正です。")
            if override.day is not None and type(override.day) is not date:
                errors.append(f"{person.name}: 個別勤務時刻の日付が不正です。")
            elif isinstance(override.shift, str):
                key = (override.day, override.shift)
                if key in seen_overrides:
                    errors.append(f"{person.name}: 同じ日付・シフトの個別勤務時刻が重複しています。")
                seen_overrides.add(key)
                if override.day is not None and valid_dates is not None and override.day not in valid_dates:
                    errors.append(f"{person.name}: 個別勤務時刻の日付は作成期間内にしてください。")
                elif (override.day is not None and merges_valid and override.shift in settings.shift_names
                      and representative_shift(settings, override.day, override.shift) != override.shift):
                    errors.append(f"{person.name}: {override.day} は「{shift_label(settings, override.day, override.shift)}」に統合されています。"
                                  f"個別勤務時刻は「{representative_shift(settings, override.day, override.shift)}」で指定してください。")
    if len(settings.avoid_pairs) > 200:
        errors.append("回避ペアは200組以内にしてください。")
    seen_pairs = set()
    for pair in settings.avoid_pairs:
        if not isinstance(pair, AvoidPair):
            errors.append("回避ペアはAvoidPairで指定してください。")
            continue
        if pair.employee_id_a == pair.employee_id_b:
            errors.append("回避ペアに同じ職員は指定できません。")
        if pair.employee_id_a not in ids or pair.employee_id_b not in ids:
            errors.append("回避ペアに未登録の職員IDがあります。")
        if type(pair.weight) is not int or not 1 <= pair.weight <= 100:
            errors.append("回避ペアの重みは1〜100の整数で指定してください。")
        if isinstance(pair.employee_id_a, str) and isinstance(pair.employee_id_b, str):
            key = frozenset((pair.employee_id_a, pair.employee_id_b))
            if key in seen_pairs:
                errors.append("回避ペアが重複しています（逆順も同じ組です）。")
            seen_pairs.add(key)
    if not isinstance(settings.same_shift_pairs, tuple):
        errors.append("同一シフトペアはSameShiftPairのタプルで指定してください。")
    else:
        if len(settings.same_shift_pairs) > 200:
            errors.append("同一シフトペアは200組以内にしてください。")
        seen_same_pairs = set()
        for pair in settings.same_shift_pairs:
            if not isinstance(pair, SameShiftPair):
                errors.append("同一シフトペアはSameShiftPairで指定してください。")
                continue
            if pair.employee_id_a == pair.employee_id_b:
                errors.append("同一シフトペアに同じ職員は指定できません。")
            if pair.employee_id_a not in ids or pair.employee_id_b not in ids:
                errors.append("同一シフトペアに未登録の職員IDがあります。")
            if pair.mode not in ("hard", "soft"):
                errors.append("同一シフトペアの条件はhardまたはsoftで指定してください。")
            if pair.direction not in ("a_to_b", "both"):
                errors.append("同一シフトペアの適用範囲はa_to_bまたはbothで指定してください。")
            if type(pair.weight) is not int or not 1 <= pair.weight <= 100:
                errors.append("同一シフトペアの重みは1〜100の整数で指定してください。")
            if isinstance(pair.employee_id_a, str) and isinstance(pair.employee_id_b, str):
                key = frozenset((pair.employee_id_a, pair.employee_id_b))
                if key in seen_same_pairs:
                    errors.append("同一シフトペアが重複しています（逆順も同じ組です）。双方向は適用範囲で指定してください。")
                seen_same_pairs.add(key)
    return errors


def _staffing_rule_errors(settings: Settings, valid_dates: set[date] | None) -> list[str]:
    if not isinstance(settings.staffing_rules, tuple):
        return ["必要人数の設定はStaffingRuleのタプルで指定してください。"]
    errors = []
    seen = set()
    for rule in settings.staffing_rules:
        if not isinstance(rule, StaffingRule):
            errors.append("必要人数の設定はStaffingRuleで指定してください。")
            continue
        target = (f"{rule.day}" if rule.day is not None else
                  f"毎週{'月火水木金土日'[rule.weekday]}曜" if type(rule.weekday) is int and 0 <= rule.weekday <= 6 else "毎日")
        label = f"必要人数（{target}・{rule.shift or '全シフト'}）"
        if rule.day is not None and rule.weekday is not None:
            errors.append(f"{label}: 日付と曜日は片方だけ指定してください。")
        if rule.day is not None and (type(rule.day) is not date or (valid_dates is not None and rule.day not in valid_dates)):
            errors.append(f"{label}: 日付は作成期間内にしてください。")
        if rule.weekday is not None and (type(rule.weekday) is not int or not 0 <= rule.weekday <= 6):
            errors.append(f"{label}: 曜日は月曜0〜日曜6の整数で指定してください。")
        if rule.shift is not None and rule.shift not in settings.shift_names:
            errors.append(f"{label}: シフトが現在のシフト一覧にありません。")
        if type(rule.minimum) is not int or not 0 <= rule.minimum <= 100:
            errors.append(f"{label}: 最低人数は0〜100の整数で指定してください。")
        elif rule.maximum is not None and (type(rule.maximum) is not int or not rule.minimum <= rule.maximum <= 100):
            errors.append(f"{label}: 最大人数は最低人数以上・100以下の整数にしてください（上限なしは空欄）。")
        key = (rule.day, rule.weekday, rule.shift)
        if key in seen:
            errors.append(f"{label}: 同じ対象の設定が重複しています。")
        seen.add(key)
    return errors


def _merge_errors(settings: Settings, valid_dates: set[date] | None) -> list[str]:
    if not isinstance(settings.shift_merges, tuple):
        return ["シフト統合はShiftMergeのタプルで指定してください。"]
    errors = []
    used: set[tuple[date, str]] = set()
    times_valid = len(settings.shift_times) == len(settings.shift_names) and all(
        _valid_timing(timing) for timing in settings.shift_times)
    for merge in settings.shift_merges:
        if not isinstance(merge, ShiftMerge):
            errors.append("シフト統合はShiftMergeで指定してください。")
            continue
        if type(merge.day) is not date or (valid_dates is not None and merge.day not in valid_dates):
            errors.append("シフト統合の日付は作成期間内の日付にしてください。")
            continue
        label = f"シフト統合 {merge.day}"
        if (not isinstance(merge.shifts, tuple) or len(merge.shifts) < 2
                or len(set(merge.shifts)) != len(merge.shifts)
                or any(name not in settings.shift_names for name in merge.shifts)):
            errors.append(f"{label}: 統合するシフトは現在のシフト一覧から重複なく2つ以上選んでください。")
            continue
        overlap = [name for name in merge.shifts if (merge.day, name) in used]
        if overlap:
            errors.append(f"{label}: 「{'・'.join(overlap)}」が同じ日の別の統合と重複しています。")
        used.update((merge.day, name) for name in merge.shifts)
        if merge.break_minutes is not None and type(merge.break_minutes) is not int:
            errors.append(f"{label}: 休憩は整数分で指定してください。")
            continue
        if not times_valid:
            continue
        timing = _merged_timing(settings, merge)
        if timing is None:
            errors.append(f"{label}: 統合するシフトの時間が途切れています。時間が連続または重なるシフトを選んでください。")
        elif not _valid_timing(timing):
            errors.append(f"{label}: 統合後の勤務長は24時間以内、休憩は0分以上で勤務長未満にしてください。")
    return errors


def capacity_issues(employees: list[Employee], settings: Settings) -> list[str]:
    """明らかな不足のみを検出する。これ以外の矛盾はソルバーで判定する。"""
    issues = []
    has_role_requirements = bool(settings.role_requirements)
    for day in settings.dates:
        if day in settings.closed_days:
            continue
        shifts_today = day_shift_names(settings, day)
        minimums = {shift: staff_bounds(settings, day, shift)[0] for shift in shifts_today}
        required = sum(minimums.values())
        leaders_required = sum(value > 0 for value in minimums.values()) if settings.leader_mode == "each_shift" else 1
        available = [person for person in employees if day not in person.days_off]
        if len(available) < required:
            issues.append(f"{day:%Y/%m/%d}: 勤務可能者は{len(available)}人、必要人数は{required}人です。")
        if not has_role_requirements:
            leaders = sum(person.is_leader for person in available)
            if leaders < leaders_required:
                issues.append(f"{day:%Y/%m/%d}: 主任以上は{leaders}人、必要人数は{leaders_required}人です。")
        else:
            for requirement in settings.role_requirements:
                if requirement.required_on != "each_day":
                    continue
                role_available = {
                    person.employee_id for person in employees
                    if employee_has_role(person, requirement.name)
                    and any(_eligible(person, settings, day, shift) for shift in shifts_today)
                }
                if len(role_available) < requirement.minimum:
                    issues.append(
                        f"{day:%Y/%m/%d}: 役割「{requirement.name}」の勤務可能者は"
                        f"{len(role_available)}人、1日あたり必要人数は{requirement.minimum}人です。"
                    )
        for shift in shifts_today:
            minimum = minimums[shift]
            if minimum == 0:
                # 0人でもよいシフトの役職要件は、誰かが入る場合だけ課す。
                continue
            eligible = [person for person in available if _eligible(person, settings, day, shift)]
            if len(eligible) < minimum:
                issues.append(f"{day:%Y/%m/%d} {shift_label(settings, day, shift)}: 固定勤務・希望休を考慮した勤務可能者は{len(eligible)}人、必要人数は{minimum}人です。")
            if not has_role_requirements and settings.leader_mode == "each_shift" and not any(person.is_leader for person in eligible):
                issues.append(f"{day:%Y/%m/%d} {shift}: 配置可能な主任以上がいません。")
            leader_ids = {person.employee_id for person in employees if person.is_leader}
            for start, end, ids in coverage_segments(employees, settings, day, shift):
                if len(ids) < minimum:
                    issues.append(f"{day:%Y/%m/%d} {shift} {start}–{end}分: 個別勤務時刻を考慮した時間帯の最低人数が不足しています。")
                if not has_role_requirements and settings.leader_mode == "each_shift" and not leader_ids.intersection(ids):
                    issues.append(f"{day:%Y/%m/%d} {shift} {start}–{end}分: 個別勤務時刻を考慮した時間帯に主任以上が不在です。")
                if has_role_requirements:
                    for requirement in settings.role_requirements:
                        if requirement.required_on != "each_shift":
                            continue
                        role_count = sum(
                            employee_has_role(person, requirement.name)
                            for person in employees if person.employee_id in ids
                        )
                        if role_count < requirement.minimum:
                            issues.append(
                                f"{day:%Y/%m/%d} {shift} {start}–{end}分: 役割「{requirement.name}」"
                                f"の時間帯勤務可能者は{role_count}人、必要人数は{requirement.minimum}人です。"
                            )
            if has_role_requirements:
                for requirement in settings.role_requirements:
                    if requirement.required_on != "each_shift":
                        continue
                    role_count = sum(employee_has_role(person, requirement.name) for person in eligible)
                    if role_count < requirement.minimum:
                        issues.append(
                            f"{day:%Y/%m/%d} {shift}: 役割「{requirement.name}」の勤務可能者は"
                            f"{role_count}人、必要人数は{requirement.minimum}人です。"
                        )
    return issues


def audit_schedule(
    employees: list[Employee], settings: Settings, assignments: tuple[Assignment, ...]
) -> list[str]:
    """ソルバーから独立した、出力シフトの制約再検査。"""
    errors = []
    input_errors = validate_inputs(employees, settings)
    if input_errors:
        return input_errors
    people = {person.employee_id: person for person in employees}
    by_person_day: dict[tuple[str, date], str] = {}
    by_day_shift: dict[tuple[date, str], list[str]] = {}
    for assignment in assignments:
        if assignment.employee_id not in people or assignment.day not in settings.dates or assignment.shift not in settings.shift_names:
            errors.append("未登録の職員・日付・シフトが含まれています。")
            continue
        key = (assignment.employee_id, assignment.day)
        if key in by_person_day:
            errors.append("同じ職員に1日2勤務以上が割り当てられています。")
        by_person_day[key] = assignment.shift
        by_day_shift.setdefault((assignment.day, assignment.shift), []).append(assignment.employee_id)
        person = people[assignment.employee_id]
        timing = employee_shift_time(person, settings, assignment.day, assignment.shift)
        if assignment.day in settings.closed_days:
            errors.append(f"{assignment.day}: 休日に勤務があります。")
        elif assignment.shift not in day_shift_names(settings, assignment.day):
            errors.append(f"{assignment.day} {assignment.shift}: 統合により配置対象外のシフトに勤務があります。")
        if not _allowed(person, settings, assignment.day, assignment.shift):
            errors.append(f"{person.name}: 固定・許可シフト以外の勤務があります。")
        if assignment.day in people[assignment.employee_id].days_off or (
            timing.end_minute > 1440
            and assignment.day + timedelta(days=1) in people[assignment.employee_id].days_off
        ):
            errors.append("休み希望日に勤務があります。")
    for day in settings.dates:
        for shift in day_shift_names(settings, day):
            staff = by_day_shift.get((day, shift), [])
            minimum, maximum = staff_bounds(settings, day, shift)
            if len(staff) < minimum:
                errors.append(f"{day} {shift}: 最低人数が不足しています。")
            if maximum is not None and len(staff) > maximum:
                errors.append(f"{day} {shift}: 最大人数（{maximum}人）を超えています。")
            if not staff and minimum == 0:
                continue
            if not settings.role_requirements and settings.leader_mode == "each_shift" and not any(people[pid].is_leader for pid in staff):
                errors.append(f"{day} {shift}: 主任以上が不在です。")
            if settings.role_requirements:
                unique_staff = set(staff)
                for requirement in settings.role_requirements:
                    if requirement.required_on != "each_shift":
                        continue
                    count = sum(employee_has_role(people[pid], requirement.name) for pid in unique_staff)
                    if count < requirement.minimum:
                        errors.append(
                            f"{day} {shift}: 役割「{requirement.name}」の配置人数が不足しています。"
                            f"必要人数は{requirement.minimum}人です。"
                        )
            for start, end, ids in coverage_segments(employees, settings, day, shift):
                covered = set(staff).intersection(ids)
                if len(covered) < minimum:
                    errors.append(f"{day} {shift} {start}–{end}分: 個別勤務時刻を考慮した時間帯の最低人数が不足しています。")
                if not settings.role_requirements and settings.leader_mode == "each_shift" and not any(people[pid].is_leader for pid in covered):
                    errors.append(f"{day} {shift} {start}–{end}分: 時間帯に主任以上が不在です。")
                if settings.role_requirements:
                    for requirement in settings.role_requirements:
                        if requirement.required_on != "each_shift":
                            continue
                        count = sum(employee_has_role(people[pid], requirement.name) for pid in covered)
                        if count < requirement.minimum:
                            errors.append(
                                f"{day} {shift} {start}–{end}分: 役割「{requirement.name}」の配置人数が不足しています。"
                                f"必要人数は{requirement.minimum}人です。"
                            )
        if day in settings.closed_days:
            continue
        if not settings.role_requirements and settings.leader_mode == "each_day" and not any(
            people[pid].is_leader for shift in settings.shift_names for pid in by_day_shift.get((day, shift), [])
        ):
            errors.append(f"{day}: 主任以上が不在です。")
        if settings.role_requirements:
            daily_staff = {
                pid for shift in settings.shift_names for pid in by_day_shift.get((day, shift), [])
            }
            for requirement in settings.role_requirements:
                if requirement.required_on != "each_day":
                    continue
                count = sum(employee_has_role(people[pid], requirement.name) for pid in daily_staff)
                if count < requirement.minimum:
                    errors.append(
                        f"{day}: 役割「{requirement.name}」の1日配置人数が不足しています。"
                        f"必要人数は{requirement.minimum}人です。"
                    )
    for pair, day, shift, source in same_shift_violations(assignments, settings):
        if pair.mode == "hard":
            errors.append(f"{day} {shift}: 同一シフトペア（{pair.employee_id_a}・{pair.employee_id_b}）の必須条件違反です。{source}の相手が不在です。")
    for person in employees:
        consecutive = 0
        previous_end = None
        previous_start_minute = None
        for day in settings.dates:
            shift = by_person_day.get((person.employee_id, day))
            for fixed in person.weekly_assignments:
                if (day.weekday() == fixed.weekday and day not in settings.closed_days
                        and shift != representative_shift(settings, day, fixed.shift)):
                    errors.append(f"{person.name} {day}: 曜日固定の「{fixed.shift}」が配置されていません。")
            consecutive = consecutive + 1 if shift else 0
            if consecutive > settings.max_consecutive_days:
                errors.append(f"{person.name}: 連続勤務上限を超えています。")
            if shift is not None:
                timing = employee_shift_time(person, settings, day, shift)
                if (settings.nondecreasing_start_times and previous_start_minute is not None
                        and timing.start_minute < previous_start_minute):
                    errors.append(f"{person.name} {day}: 出勤時刻が前日より早くなっています。")
                previous_start_minute = timing.start_minute
                start = (day - settings.start_date).days * 1440 + timing.start_minute
                if previous_end is not None and start - previous_end < settings.minimum_rest_hours * 60:
                    errors.append(f"{person.name}: 勤務間の休息が不足しています。")
                previous_end = (day - settings.start_date).days * 1440 + timing.end_minute
            else:
                # 休日は開始時刻の比較だけをリセットする。休息の終了時刻は保持する。
                previous_start_minute = None
        for month, indices in month_groups(settings).items():
            month_dates = {settings.dates[d] for d in indices}
            minutes = sum(employee_shift_time(person, settings, a.day, a.shift).working_minutes
                          for a in assignments if a.employee_id == person.employee_id
                          and a.day in month_dates and a.shift in settings.shift_names)
            if person.max_monthly_minutes is not None and minutes > person.max_monthly_minutes:
                errors.append(f"{person.name} {month}: 月間労働時間の上限を超えています。")
            required = required_working_minutes(person, settings, indices)
            if required is not None and minutes != required:
                errors.append(f"{person.name} {month}: 実労働時間が指定時間と一致しません。")
    return errors


def pin_errors(
    employees: list[Employee], settings: Settings, fixed: dict[tuple[str, date], str | None],
) -> list[str]:
    """手修正で固定したマスの検査。値はシフト名、または None（休み）。"""
    errors = []
    people = {person.employee_id: person for person in employees}
    for (employee_id, day), shift in fixed.items():
        person = people.get(employee_id)
        if person is None or day not in settings.dates:
            errors.append(f"固定したマス（{employee_id} {day}）の職員または日付が作成対象にありません。")
            continue
        label = f"固定したマス（{person.name} {day:%m/%d}）"
        if shift is None:
            continue
        if shift not in settings.shift_names:
            errors.append(f"{label}: シフト「{shift}」は現在のシフト一覧にありません。")
        elif day in settings.closed_days:
            errors.append(f"{label}: 休日に勤務を固定しています。")
        elif shift not in day_shift_names(settings, day):
            errors.append(f"{label}: この日は「{shift_label(settings, day, shift)}」に統合されています。"
                          f"「{representative_shift(settings, day, shift)}」で指定してください。")
        elif not _allowed(person, settings, day, shift):
            errors.append(f"{label}: 固定・許可シフト以外の「{shift}」を指定しています。")
        elif not _eligible(person, settings, day, shift):
            errors.append(f"{label}: 希望休（前日開始で希望休にかかる勤務を含む）に勤務を固定しています。")
    return errors


def solve_schedule(
    employees: list[Employee], settings: Settings, *,
    fixed: dict[tuple[str, date], str | None] | None = None,
    previous: tuple[Assignment, ...] = (),
) -> ScheduleResult:
    """fixed のマスは必ずその値にする（None は休み）。previous を渡すと、
    固定以外のマスの変更数の最小化を最優先にして作り直す。"""
    pins = dict(fixed or {})
    # 休息制約のループ変数 previous と衝突しないよう別名で保持する。
    previous_assignments = tuple(previous)
    errors = validate_inputs(employees, settings)
    if not errors:
        errors = pin_errors(employees, settings, pins)
    if errors:
        return ScheduleResult("invalid", messages=tuple(errors))
    issues = capacity_issues(employees, settings)
    if issues:
        if any(person.weekly_assignments for person in employees):
            issues.append("曜日固定を含む必須条件と必要人数・役割配置を同時に満たせません。")
        return ScheduleResult("infeasible", messages=tuple(issues))

    model = cp_model.CpModel()
    dates = settings.dates
    staff_indices = range(len(employees))
    day_indices = range(settings.days)
    shift_indices = range(len(settings.shift_names))
    x = {
        (p, d, s): model.new_bool_var(f"work_{p}_{d}_{s}")
        for p in staff_indices for d in day_indices for s in shift_indices
    }
    timings = {
        (p, d, s): employee_shift_time(person, settings, day, shift)
        for p, person in enumerate(employees) for d, day in enumerate(dates)
        for s, shift in enumerate(settings.shift_names)
    }
    work = {}
    for p, person in enumerate(employees):
        for d, day in enumerate(dates):
            work[p, d] = model.new_bool_var(f"day_{p}_{d}")
            model.add(sum(x[p, d, s] for s in shift_indices) == work[p, d])
            if day in person.days_off:
                model.add(work[p, d] == 0)
            for fixed in person.weekly_assignments:
                # 休日の曜日固定は適用しない。
                if day.weekday() == fixed.weekday and day not in settings.closed_days:
                    fixed_shift = representative_shift(settings, day, fixed.shift)
                    model.add(x[p, d, settings.shift_names.index(fixed_shift)] == 1)
            for s in shift_indices:
                if not _eligible(person, settings, day, settings.shift_names[s]):
                    model.add(x[p, d, s] == 0)
            if (person.employee_id, day) in pins:
                pinned = pins[person.employee_id, day]
                if pinned is None:
                    model.add(work[p, d] == 0)
                else:
                    model.add(x[p, d, settings.shift_names.index(pinned)] == 1)
        window = settings.max_consecutive_days + 1
        for start in range(settings.days - window + 1):
            model.add(sum(work[p, d] for d in range(start, start + window)) <= settings.max_consecutive_days)
        # 翌日終了の長い勤務では、中1日空いても休息不足になり得る。
        for gap in (1, 2):
            for d in range(settings.days - gap):
                for previous in shift_indices:
                    for following in shift_indices:
                        rest = gap * 1440 + timings[p, d + gap, following].start_minute - timings[p, d, previous].end_minute
                        earlier_start = (
                            settings.nondecreasing_start_times and gap == 1
                            and timings[p, d + gap, following].start_minute < timings[p, d, previous].start_minute
                        )
                        if rest < settings.minimum_rest_hours * 60 or earlier_start:
                            model.add(x[p, d, previous] + x[p, d + gap, following] <= 1)

    leaders = [p for p, person in enumerate(employees) if person.is_leader]
    person_indices = {person.employee_id: p for p, person in enumerate(employees)}
    for d in day_indices:
        shifts_today = day_shift_names(settings, dates[d])
        for s in shift_indices:
            if settings.shift_names[s] not in shifts_today:
                continue
            minimum, maximum = staff_bounds(settings, dates[d], settings.shift_names[s])
            model.add(sum(x[p, d, s] for p in staff_indices) >= minimum)
            if maximum is not None:
                model.add(sum(x[p, d, s] for p in staff_indices) <= maximum)
            # 0人でもよいシフトは、誰かが入った場合だけ役職要件を課す。
            if minimum > 0:
                active = 1
            else:
                active = model.new_bool_var(f"active_{d}_{s}")
                for p in staff_indices:
                    model.add(x[p, d, s] <= active)
            if not settings.role_requirements and settings.leader_mode == "each_shift":
                model.add(sum(x[p, d, s] for p in leaders) >= active)
            for _, _, ids in coverage_segments(employees, settings, dates[d], settings.shift_names[s]):
                covered = [person_indices[pid] for pid in ids]
                model.add(sum(x[p, d, s] for p in covered) >= minimum)
                if not settings.role_requirements and settings.leader_mode == "each_shift":
                    model.add(sum(x[p, d, s] for p in covered if employees[p].is_leader) >= active)
                if settings.role_requirements:
                    for requirement in settings.role_requirements:
                        if requirement.required_on != "each_shift":
                            continue
                        members = [p for p in covered if employee_has_role(employees[p], requirement.name)]
                        model.add(sum(x[p, d, s] for p in members) >= requirement.minimum * active)
            if settings.role_requirements:
                for requirement in settings.role_requirements:
                    if requirement.required_on != "each_shift":
                        continue
                    members = [p for p in staff_indices if employee_has_role(employees[p], requirement.name)]
                    model.add(sum(x[p, d, s] for p in members) >= requirement.minimum * active)
        if dates[d] in settings.closed_days:
            continue
        if not settings.role_requirements and settings.leader_mode == "each_day":
            model.add(sum(x[p, d, s] for p in leaders for s in shift_indices) >= 1)
        if settings.role_requirements:
            for requirement in settings.role_requirements:
                if requirement.required_on != "each_day":
                    continue
                members = [p for p in staff_indices if employee_has_role(employees[p], requirement.name)]
                model.add(sum(x[p, d, s] for p in members for s in shift_indices) >= requirement.minimum)

    # 指定実労働時間と月間上限は常にハード制約。未設定なら等式を付けない。
    for p, person in enumerate(employees):
        for indices in month_groups(settings).values():
            total = sum(x[p, d, s] * timings[p, d, s].working_minutes for d in indices for s in shift_indices)
            if person.max_monthly_minutes is not None:
                model.add(total <= person.max_monthly_minutes)
            required = required_working_minutes(person, settings, indices)
            if required is not None:
                model.add(total == required)

    # 辞書式優先順位: 配置数 → 重み付きペア条件＋勤務帯回避 → 勤務回数の偏り。
    ranges = []
    for shift in (None, *shift_indices):
        counts = []
        for p in staff_indices:
            count = model.new_int_var(0, settings.days, f"count_{p}_{shift}")
            terms = [work[p, d] for d in day_indices] if shift is None else [x[p, d, shift] for d in day_indices]
            model.add(count == sum(terms))
            counts.append(count)
        high = model.new_int_var(0, settings.days, f"high_{shift}")
        low = model.new_int_var(0, settings.days, f"low_{shift}")
        model.add_max_equality(high, counts)
        model.add_min_equality(low, counts)
        ranges.append(high - low)
    fairness_bound = (len(settings.shift_names) + 1) * settings.days
    pair_terms = []
    for i, pair in enumerate(settings.avoid_pairs):
        a, b = person_indices[pair.employee_id_a], person_indices[pair.employee_id_b]
        for d in day_indices:
            for s in shift_indices:
                conflict = model.new_bool_var(f"pair_{i}_{d}_{s}")
                model.add(conflict <= x[a, d, s])
                model.add(conflict <= x[b, d, s])
                model.add(conflict >= x[a, d, s] + x[b, d, s] - 1)
                pair_terms.append(pair.weight * conflict)
    pair_max_bound = sum(pair.weight for pair in settings.avoid_pairs) * settings.days * len(settings.shift_names)
    for i, pair in enumerate(settings.same_shift_pairs):
        a, b = person_indices[pair.employee_id_a], person_indices[pair.employee_id_b]
        directions = [(a, b)] if pair.direction == "a_to_b" else [(a, b), (b, a)]
        for source, target in directions:
            for d in day_indices:
                for s in shift_indices:
                    if pair.mode == "hard":
                        model.add(x[source, d, s] <= x[target, d, s])
                    else:
                        missing = model.new_bool_var(f"same_pair_{i}_{source}_{d}_{s}")
                        model.add(missing >= x[source, d, s] - x[target, d, s])
                        model.add(missing <= x[source, d, s])
                        model.add(missing + x[target, d, s] <= 1)
                        pair_terms.append(pair.weight * missing)
        if pair.mode == "soft":
            pair_max_bound += pair.weight * settings.days * len(directions)
    preference_terms = [
        person.shift_avoidance_weight * x[p, d, s]
        for p, person in enumerate(employees) for d in day_indices
        for s, shift in enumerate(settings.shift_names)
        if person.avoided_shifts.intersection(merged_shifts(settings, dates[d], shift))
    ]
    # 1人1日1勤務なので、回避シフト数には依存しない安全な上界。
    preference_bound = sum(person.shift_avoidance_weight * settings.days
                           for person in employees if person.avoided_shifts)
    pair_weight = fairness_bound + 1
    count_weight = (pair_max_bound + preference_bound) * pair_weight + fairness_bound + 1
    objective = (count_weight * sum(x.values())
                 + pair_weight * (sum(pair_terms) + sum(preference_terms)) + sum(ranges))
    if previous_assignments:
        # 作り直しでは、固定以外のマスを元のシフトからなるべく変えないことを最優先にする。
        before = {(a.employee_id, a.day): a.shift for a in previous_assignments}
        changes = []
        for p, person in enumerate(employees):
            for d, day in enumerate(dates):
                if (person.employee_id, day) in pins:
                    continue
                old = before.get((person.employee_id, day))
                if old in settings.shift_names:
                    s_old = settings.shift_names.index(old)
                    changes.append(1 - x[p, d, s_old])
                    model.add_hint(x[p, d, s_old], 1)
                else:
                    changes.append(work[p, d])
                    model.add_hint(work[p, d], 0)
        objective_bound = (count_weight * len(x) + pair_weight * (pair_max_bound + preference_bound)
                           + fairness_bound)
        objective = (objective_bound + 1) * sum(changes) + objective
    model.minimize(objective)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = settings.time_limit_seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42
    status = solver.solve(model)
    elapsed = solver.wall_time
    if status == cp_model.INFEASIBLE:
        return ScheduleResult("infeasible", messages=(
            "全条件を同時に満たすシフトはありません。日・曜日・シフトごとの必要人数（最低・最大）・休日・シフト統合・同一シフトペア（ハード）・曜日固定・個別勤務時刻による時間帯の配置・月間実労働時間（日割り後の指定時間と完全一致が必要）・月間上限・固定シフト・休み希望・役割別配置人数または主任以上の人数・連続勤務上限・勤務間の休息・前日より早い出勤の禁止を確認してください。",
            "これは条件の組み合わせによる判定であり、どの条件だけが原因かを特定したものではありません。",
            *(("手修正で固定したマスが他の条件と両立していない可能性があります。固定を一部解除して再度お試しください。",)
              if pins else ()),
        ), elapsed_seconds=elapsed)
    if status == cp_model.MODEL_INVALID:
        return ScheduleResult("invalid", messages=("最適化モデルが不正です。設定を確認してください。",), elapsed_seconds=elapsed)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return ScheduleResult("unknown", messages=(
            "時間内にシフトが見つかりませんでした。作成不可とは断定できません。計算時間を延ばすか対象期間を短くしてください。",
        ), elapsed_seconds=elapsed)
    assignments = tuple(
        Assignment(employees[p].employee_id, dates[d], settings.shift_names[s])
        for d in day_indices for s in shift_indices for p in staff_indices
        if solver.value(x[p, d, s])
    )
    audit_errors = audit_schedule(employees, settings, assignments)
    if audit_errors:
        return ScheduleResult("invalid", messages=tuple(audit_errors), elapsed_seconds=elapsed)
    return ScheduleResult(
        "optimal" if status == cp_model.OPTIMAL else "feasible",
        assignments=assignments, elapsed_seconds=elapsed,
    )