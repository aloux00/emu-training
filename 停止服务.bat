@echo off
title 停止教学排课系统
set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do set "FOUND=%%p"
if not defined FOUND (
    echo 当前没有运行中的服务（8000 端口无监听）。
    echo.
    pause
    exit /b 0
)
taskkill /F /PID %FOUND% >nul 2>&1
echo 已停止服务（进程 %FOUND%）。
echo.
pause
