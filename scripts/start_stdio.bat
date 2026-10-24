@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
REM ===================================================================
REM Native-CLI-Orchestrator - stdio 子进程模式（DSH / Cherry Studio 本地 MCP）
REM
REM 注意：stdio 下 stdout 是 MCP 协议通道，不要在本窗口手动输入。
REM 该模式一般由上层工作台自动拉起，手动运行仅用于排错。
REM 用法：start_stdio.bat [mode]
REM ===================================================================
setlocal
cd /d "%~dp0.."

set MODE=%1

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

set ARGS=
if not "%MODE%"=="" set ARGS=--mode %MODE%

echo [Native-CLI-Orchestrator] 启动 stdio 模式（由上层工作台接管标准输入输出）...
echo   手动联调请用：python scripts\mcp_client_demo.py stdio
echo.

"%PY%" -m native_cli_orchestrator.entry.stdio --config config\config.yaml %ARGS%
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" echo [启动失败] 退出码=%EXITCODE%

endlocal & exit /b %EXITCODE%
