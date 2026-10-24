@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
REM ===================================================================
REM 环境自检（不启动服务）
REM   scripts\doctor.bat          仅自检
REM   scripts\doctor.bat deep     自检 + 探测 API base_url 连通性
REM ===================================================================
setlocal
cd /d "%~dp0.."

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

if /i "%1"=="deep" (
  "%PY%" -m native_cli_orchestrator.entry --doctor --deep
) else (
  "%PY%" -m native_cli_orchestrator.entry --doctor
)
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
  echo 自检未通过。修好 [ERROR] 项后重跑本脚本。
) else (
  echo 自检通过，可以启动服务。
)
endlocal & exit /b %EXITCODE%
