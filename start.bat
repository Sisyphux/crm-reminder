@echo off
title 客户跟进提醒系统
cd /d "%~dp0"
set "PY=%CD%\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo 正在创建虚拟环境...
    python -m venv venv
    if %errorlevel% neq 0 (echo 创建失败 & pause & exit /b 1)
    echo 正在安装依赖（首次运行需下载包，请稍候）...
    "%CD%\venv\Scripts\python.exe" -m pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo 依赖安装失败，请手动运行:
        echo   %CD%\venv\Scripts\python.exe -m pip install -r requirements.txt
        pause & exit /b 1
    )
)

echo.
echo ============================
echo   客户跟进提醒系统
echo ============================
echo.
echo 正在启动服务，请稍候...
echo 服务就绪后将自动打开浏览器
echo 按 Ctrl+C 停止服务
echo.

:: 启动 Flask 服务（后台运行）
start "" /B "%PY%" app.py > "%TEMP%\crm_server.log" 2>&1

:: 等待服务就绪（最多等 15 秒）
set "READY="
for /l %%i in (1,1,15) do (
    >nul 2>&1 powershell -NoProfile -Command "try{$c=New-Object System.Net.Sockets.TcpClient;$c.Connect('127.0.0.1',8080);$c.Close();$true}catch{$false}" && set READY=1
    if defined READY goto ready
    >nul ping -n 2 127.0.0.1
)
:ready

if defined READY (
    echo 服务已就绪，正在打开浏览器...
    start http://localhost:8080
) else (
    echo 警告：服务启动较慢，请手动打开 http://localhost:8080
    start http://localhost:8080
)

:: 保持窗口打开，方便用户按 Ctrl+C 停止
"%PY%" -c "import sys; sys.stdin.read()" 2>nul || pause
