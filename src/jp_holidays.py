"""日本の国民の祝日（振替休日・国民の休日を含む）。外部ライブラリ・通信なしで計算する。

2016年以降の祝日法に対応（山の日の新設以降）。2019年の天皇即位関連と、
2020・2021年の東京五輪に伴う移動も反映する。春分・秋分は天文計算の近似式（1980〜2099年）。
"""

from datetime import date, timedelta
from functools import lru_cache


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7 + 7 * (n - 1))


def _equinox_day(year: int, base: float) -> int:
    return int(base + 0.242194 * (year - 1980) - (year - 1980) // 4)


def _base_holidays(year: int) -> dict[date, str]:
    days = {
        date(year, 1, 1): "元日",
        _nth_monday(year, 1, 2): "成人の日",
        date(year, 2, 11): "建国記念の日",
        date(year, 3, _equinox_day(year, 20.8431)): "春分の日",
        date(year, 4, 29): "昭和の日",
        date(year, 5, 3): "憲法記念日",
        date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日",
        _nth_monday(year, 9, 3): "敬老の日",
        date(year, 9, _equinox_day(year, 23.2488)): "秋分の日",
        date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
    }
    if year >= 2020:
        days[date(year, 2, 23)] = "天皇誕生日"
    elif year <= 2018:
        days[date(year, 12, 23)] = "天皇誕生日"
    moved = {
        2020: (date(2020, 7, 23), date(2020, 8, 10), date(2020, 7, 24)),
        2021: (date(2021, 7, 22), date(2021, 8, 8), date(2021, 7, 23)),
    }
    if year in moved:
        marine, mountain, sports = moved[year]
    else:
        marine, mountain, sports = _nth_monday(year, 7, 3), date(year, 8, 11), _nth_monday(year, 10, 2)
    days[marine] = "海の日"
    if year >= 2016:
        days[mountain] = "山の日"
    days[sports] = "スポーツの日" if year >= 2020 else "体育の日"
    if year == 2019:
        days[date(2019, 5, 1)] = "天皇の即位の日"
        days[date(2019, 10, 22)] = "即位礼正殿の儀の行われる日"
    return days


@lru_cache(maxsize=None)
def holidays_in_year(year: int) -> dict[date, str]:
    days = _base_holidays(year)
    # 国民の休日：前日と翌日が祝日に挟まれた平日。
    for day in sorted(days):
        between = day + timedelta(days=1)
        if between not in days and between + timedelta(days=1) in days and between.weekday() != 6:
            days[between] = "国民の休日"
    # 振替休日：日曜の祝日の後で最も近い祝日でない日。
    for day in sorted(days):
        if day.weekday() == 6 and days[day] != "振替休日":
            substitute = day + timedelta(days=1)
            while substitute in days:
                substitute += timedelta(days=1)
            if substitute.year == year:
                days[substitute] = "振替休日"
    return dict(sorted(days.items()))


def holiday_name(day: date) -> str | None:
    return holidays_in_year(day.year).get(day)


def default_closed_days(dates) -> frozenset[date]:
    """土日・祝日を休日とする初期値。"""
    return frozenset(day for day in dates if day.weekday() >= 5 or holiday_name(day) is not None)
