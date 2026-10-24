@echo off
chcp 936 >nul
REM ===================================================================
REM 打包交付目录（文件夹方式，默认）
REM 产物：dist\Native-CLI-Orchestrator\
REM 排除运行期垃圾：.venv / __pycache__ / workspace / backups / logs / probe-report
REM ===================================================================
setlocal
cd /d "%~dp0.."

set SRC=%CD%
set OUT=%SRC%\dist\Native-CLI-Orchestrator

echo [打包] 源: %SRC%
echo [打包] 目标: %OUT%

if exist "%OUT%" rmdir /s /q "%OUT%"
mkdir "%OUT%" 2>nul

REM /XD 同时接受完整路径与纯目录名，纯目录名匹配任意层级
robocopy "%SRC%" "%OUT%" /E /NFL /NDL /NJH /NJS /NP ^
 /XD ".venv" "__pycache__" ".git" ".ai-workspace-runtime" "dist" "build" ^
     "workspace" "backups" "logs" "probe-report" ".pytest_cache" ".mypy_cache" ".ruff_cache" ^
 /XF "*.pyc" "*.log" ".pools.state.json" "config.local.yaml" "*.spec" >nul

if errorlevel 8 (
  echo [失败] robocopy 返回 %ERRORLEVEL%
  exit /b 1
)

echo.
echo [检查] 不应出现运行期目录与缓存（无输出 = 干净）：
dir /s /b "%OUT%" | findstr /i "\\workspace\\ \\backups\\ \\logs\\ \\probe-report\\ \\.venv\\ __pycache__ .pools.state.json"

echo.
echo [完成] 已打包到 %OUT%
echo 迁移到工作机后：
echo   1^) pip install -r requirements.txt ^&^& pip install -e .
echo   2^) 配环境变量（TRAE_TOKEN_1 等）
echo   3^) 改 config\accounts.yml 与 config\config.yaml 的 mode: real
echo   4^) scripts\start_sse.bat
endlocal
