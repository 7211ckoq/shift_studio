@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python virtual environment not found. See README.md for setup.
    pause
    exit /b 1
)
echo Shift Studio: http://127.0.0.1:8501
start "" "http://127.0.0.1:8501"
".venv\Scripts\python.exe" -m streamlit run src/app.py --server.address 127.0.0.1 --server.port 8501
if errorlevel 1 pause