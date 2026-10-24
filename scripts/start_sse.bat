@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
REM ===================================================================
REM Native-CLI-Orchestrator - SSE 服务模式（主推）
REM 用法：start_sse.bat [port] [mode]
REM   例：start_sse.bat             -> 默认端口 + 配置文件的 mode
REM       start_sse.bat 8771 real   -> 指定端口并强制 real
REM ===================================================================
setlocal
cd /d "%~dp0.."

set PORT=%1
set MODE=%2

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

set ARGS=
if not "%PORT%"=="" set ARGS=%ARGS% --port %PORT%
if not "%MODE%"=="" set ARGS=%ARGS% --mode %MODE%

echo [Native-CLI-Orchestrator] 启动 SSE 服务...
echo   配置文件: config\config.yaml
echo   账号池:   config\accounts.yml
if "%PORT%"=="" (
  echo   端点:     http://127.0.0.1:8770/sse ^(来自 config.yaml^)
) else (
  echo   端点:     http://127.0.0.1:%PORT%/sse
)
echo   按 Ctrl+C 停止
echo.

"%PY%" -m native_cli_orchestrator.entry.sse --config config\config.yaml %ARGS%
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo [启动失败] 退出码=%EXITCODE%
  echo 排查建议：
  echo   1^) 依赖是否安装：pip install -r requirements.txt ^&^& pip install -e .
  echo   2^) 配置是否正确：%PY% -m native_cli_orchestrator.entry sse --print-config
  echo   3^) 端口是否被占用：netstat -ano ^| findstr 8770
)

endlocal & exit /b %EXITCODE%
