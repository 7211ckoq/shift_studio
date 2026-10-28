"""画面テスト共通の初期化。"""


def all_workdays(at):
    """土日祝の初期休日を無効にし、作成期間の全日を出勤日として起動する。"""
    at.session_state["holiday_toggles"] = set()
    at.session_state["holiday_base"] = False
    return at
