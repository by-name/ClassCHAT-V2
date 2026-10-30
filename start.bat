@echo off
chcp 65001 >nul
title Class-Chat V2 Server

cd /d "%~dp0"

echo ============================================
echo   Class-Chat V2 Startup
echo ============================================
echo.

echo [INFO] Class-CHAT版本: V2
REM ========== Python ==========
set "PYTHON="
if exist "python\python.exe" set "PYTHON=python\python.exe"
if exist "python\Scripts\python.exe" set "PYTHON=python\Scripts\python.exe"
if not defined PYTHON (
    echo [ERROR] 未找到 python 目录
    pause & exit /b 1
)
echo [INFO] Python: %PYTHON%
"%PYTHON%" --version

REM ========== websockets ==========
"%PYTHON%" -c "import websockets" 2>nul
if errorlevel 1 (
    echo [INFO] 安装 websockets...
    "%PYTHON%" -m pip install websockets==12.0 --no-deps
)

REM ========== 文件检查 ==========
if not exist "chat_websocket.py" (echo [ERROR] 未找到 chat_websocket.py & pause & exit /b 1)
if not exist "account_db.py" (echo [ERROR] 未找到 account_db.py & pause & exit /b 1)
if not exist "file" mkdir file

REM ========== 初始化账号数据库（首次运行会自动迁移 Account.json）==========
"%PYTHON%" account_db.py
if errorlevel 1 (echo [ERROR] 账号数据库初始化失败 & pause & exit /b 1)

REM ========== 显示管理员账号密码 ==========
if exist "show_admin.py" (
    "%PYTHON%" show_admin.py
) else (
    echo [WARN] 未找到 show_admin.py，跳过管理员密码显示
)

REM ========== 读取 config.json（前后端共用端口）==========
set "WS_PORT=8765"
if exist "config.json" (
    for /f "usebackq delims=" %%P in (`"%PYTHON%" -c "import json;print(json.load(open('config.json',encoding='utf-8')).get('ws_port',8765))" 2^>nul`) do set "WS_PORT=%%P"
) else (
    echo [WARN] 未找到 config.json，WebSocket 使用默认端口 8765
)


REM ========== 启动 nginx ==========
if exist "nginx\nginx.exe" (
    echo [INFO] 启动 nginx...
    cd nginx
    nginx.exe -s stop 2>nul
    timeout /t 1 >nul
    start "" nginx.exe
    cd ..
    timeout /t 2 >nul
    tasklist | findstr nginx >nul && echo [INFO] nginx OK (port 8080) || echo [WARN] nginx failed
)

REM ========== 启动 WebSocket（独立窗口）==========
echo [INFO] 启动 WebSocket 服务器...
start "Class-Chat WS" cmd /k ""%PYTHON%" chat_websocket.py"

echo.
echo ============================================
echo   All services started!
echo   Browser: http://localhost:8080
echo   WebSocket: ws://localhost:%WS_PORT%
echo   Close WS window or press any key here to stop nginx.
echo ============================================
echo.

pause >nul

echo [INFO] 关闭 nginx...
if exist "nginx\nginx.exe" (
    cd nginx
    nginx.exe -s stop 2>nul
    taskkill /f /fi "WINDOWTITLE eq Class-Chat WS*" 2>nul
    cd ..
)
echo [INFO] Done.
pause