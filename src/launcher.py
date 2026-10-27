"""配布exeの入口。ローカル限定のStreamlitを起動する。"""

import argparse
from datetime import date
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def available_port(preferred: int) -> int:
    """既存アプリには接続せず、空いている別ポートを選ぶ。"""
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", preferred))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def open_when_ready(port: int, stop: threading.Event) -> None:
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if stop.wait(0.5):
            return
        try:
            with urlopen(f"{url}/_stcore/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except (URLError, OSError):
            continue


def self_test() -> None:
    """凍結exe内でソルバー・動的UI・CSVまで実際に動かす。"""
    import pandas as pd
    from streamlit.testing.v1 import AppTest
    from optimizer import Employee, Settings, solve_schedule

    staff = [Employee(str(i), f"Sample {i}", True) for i in range(9)]
    result = solve_schedule(staff, Settings(date(2026, 1, 1), days=1))
    if not result.has_solution:
        raise RuntimeError("Solver self-test failed")
    if not pd.DataFrame({"test": [1]}).style.to_html():
        raise RuntimeError("Table self-test failed")
    app = AppTest.from_file(str(resource_root() / "app.py"), default_timeout=60).run()
    if app.exception:
        raise RuntimeError("UI self-test failed")
    next(button for button in app.button if button.label == "シフトを作成").click().run()
    if app.exception or not app.session_state.schedule[1].has_solution:
        raise RuntimeError("Schedule UI self-test failed")
    print("SELF-TEST OK: solver, UI, schedule, tables and CSV")


def main() -> int:
    parser = argparse.ArgumentParser(description="Shift Studio local application")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # 配布先の書込み権限や起動時のカレントフォルダーに依存させない。
    # 開発時はsrcの親にある.streamlit設定を使用し、凍結時は同梱設定を使用する。
    os.chdir(resource_root() if getattr(sys, "frozen", False) else resource_root().parent)
    if args.self_test:
        self_test()
        return 0
    from streamlit.web import bootstrap

    port = available_port(args.port)
    print(f"Shift Studio: http://127.0.0.1:{port}", flush=True)
    print("Keep this window open. Close it or press Ctrl+C to exit.", flush=True)
    stop = threading.Event()
    options = {
        "global.developmentMode": False,
        "logger.level": "error",
        "server.address": "127.0.0.1",
        "server.port": port,
        "server.headless": True,
        "server.fileWatcherType": "none",
        "browser.gatherUsageStats": False,
    }
    # bootstrap.run自体はCLIフラグを設定へロードしないため、先に適用する。
    bootstrap.load_config_options(options)
    if not args.no_browser:
        threading.Thread(target=open_when_ready, args=(port, stop), daemon=True).start()
    try:
        bootstrap.run(str(resource_root() / "app.py"), False, [], options)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())