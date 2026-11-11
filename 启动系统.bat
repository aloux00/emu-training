@echo off
title 教学排课系统 - 运行中
cd /d "%~dp0"
set "PY=C:/Users/Lenovo/.workbuddy/binaries/python/versions/3.13.12/python.exe"
set "PYW=C:/Users/Lenovo/.workbuddy/binaries/python/versions/3.13.12/pythonw.exe"
if not exist "%PY%" (
    echo [错误] 找不到运行环境：
    echo   %PY%
    echo 请用记事本打开本文件，把 PY 改成你电脑上装有 fastapi 的 python.exe 路径。
    pause
    exit /b 1
)

set "OLDPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do set "OLDPID=%%p"
if defined OLDPID (
    echo 服务已在运行（进程 %OLDPID%），直接打开页面...
    start "" http://127.0.0.1:8000
    ping -n 3 127.0.0.1 >nul
    exit /b 0
)

echo ==================================================
echo    教学排课系统  启动中...
echo    地址：http://127.0.0.1:8000
echo.
echo    注意：这个黑窗口不能关，关掉服务就停了
echo         （钉钉群机器人也会跟着断开）
echo    要停止服务：双击 停止服务.bat
echo ==================================================
echo.
start "" /min "%PYW%" -c "import time,webbrowser;time.sleep(4);webbrowser.open('http://127.0.0.1:8000')"
"%PY%" -m uvicorn main:app --host 127.0.0.1 --port 8000
echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
