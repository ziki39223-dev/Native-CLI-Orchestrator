@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
REM ===================================================================
REM 上游一键更新（带备份与失败回滚）
REM 用法：upstream-update.bat [check]
REM   upstream-update.bat         -> 执行更新（失败自动回滚）
REM   upstream-update.bat check   -> 只检查版本，不改任何文件
REM
REM 更新内容：CLI 命令与参数、MCP 入参出参 schema、模型清单
REM           （按 config.yaml 的 upstream 配置）
REM 备份位置：backups\upstream-时间戳\
REM ===================================================================
setlocal
cd /d "%~dp0.."

set MODE=%1
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [Native-CLI-Orchestrator] 上游更新...
if /i "%MODE%"=="check" set FLAG=--check
if /i "%MODE%"=="check" (
  echo   模式: 仅检查，不修改文件
) else (
  echo   模式: 执行更新，失败自动回滚
)
echo.

"%PY%" scripts\upstream_update.py %FLAG%
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo [更新失败] 退出码=%EXITCODE%
  echo 备份目录: backups\
  echo 需要时手动恢复：把 backups 里最新一份的 vendor 目录拷回项目根
)

endlocal & exit /b %EXITCODE%
