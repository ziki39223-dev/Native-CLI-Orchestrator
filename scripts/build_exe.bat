@echo off
chcp 936 >nul
REM ===================================================================
REM 可选：PyInstaller 单文件 exe（工作机无 Python 时用）
REM 产物：dist\orchestrator.exe
REM 用法：orchestrator.exe sse --config config\config.yaml
REM ===================================================================
setlocal
cd /d "%~dp0.."

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo [打包] 安装 pyinstaller...
"%PY%" -m pip install -q -r requirements-build.txt
if errorlevel 1 (
  echo [失败] 依赖安装失败
  exit /b 1
)

echo [打包] 生成单文件 exe...
"%PY%" -m PyInstaller ^
  --onefile ^
  --name orchestrator ^
  --clean ^
  --noconfirm ^
  --hidden-import mcp.server.mcpserver ^
  --hidden-import mcp.server.sse ^
  --hidden-import mcp.server.stdio ^
  --hidden-import mcp.client.sse ^
  --hidden-import mcp.client.stdio ^
  --collect-submodules mcp ^
  entry_exe.py

if errorlevel 1 (
  echo [失败] PyInstaller 打包失败，见 build\ 目录日志
  exit /b 1
)

echo.
echo [完成] dist\orchestrator.exe
echo 用法：orchestrator.exe sse --config config\config.yaml
echo       orchestrator.exe stdio --config config\config.yaml
endlocal
