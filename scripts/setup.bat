@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk:replace
setlocal enabledelayedexpansion
REM ===================================================================
REM Native-CLI-Orchestrator - 一键安装（开发机 / 工作机通用）
REM
REM 做四件事：
REM   1. 检查 Python 版本（需 3.12+）
REM   2. 建 .venv 并安装依赖
REM   3. 补齐缺失的 config\accounts.yml 等配置
REM   4. 跑环境自检，告诉你还差什么
REM
REM 用法：双击，或在项目根执行 scripts\setup.bat
REM ===================================================================
cd /d "%~dp0.."
echo ==============================================================
echo  Native-CLI-Orchestrator 安装
echo  项目目录：%CD%
echo ==============================================================
echo.

REM ---------------------------------------------------------------- 1. Python
echo [1/4] 检查 Python...
set PYEXE=
where python >nul 2>&1
if errorlevel 1 goto :no_python

for /f "delims=" %%v in ('python -c "import sys;print('%%d.%%d'%%sys.version_info[:2])" 2^>nul') do set PYVER=%%v
if "%PYVER%"=="" goto :no_python

for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
  set MAJ=%%a
  set MIN=%%b
)
if !MAJ! LSS 3 goto :old_python
if !MAJ! EQU 3 if !MIN! LSS 12 goto :old_python
echo       Python %PYVER%  OK
set PYEXE=python

REM ---------------------------------------------------------------- 2. venv
echo.
echo [2/4] 准备虚拟环境 .venv ...
if exist ".venv\Scripts\python.exe" (
  echo       已存在，复用
) else (
  "%PYEXE%" -m venv .venv
  if errorlevel 1 (
    echo       [失败] 创建 venv 失败
    goto :fail
  )
  echo       已创建
)
set VPY=.venv\Scripts\python.exe

echo       安装依赖（约 1-3 分钟，取决于网络）...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo       [失败] 依赖安装失败，请检查网络或换镜像：
  echo         "%VPY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  goto :fail
)
"%VPY%" -m pip install --disable-pip-version-check -q -e .
if errorlevel 1 (
  echo       [警告] 可编辑安装失败，改用 PYTHONPATH 兜底
  set PYTHONPATH=%CD%\src
) else (
  echo       依赖安装完成
)

REM ---------------------------------------------------------------- 3. 配置
echo.
echo [3/4] 检查配置文件...
"%VPY%" -m native_cli_orchestrator.entry --init
if errorlevel 1 (
  echo       [警告] 配置初始化异常，稍后可用 --init 手动补齐
)

REM ---------------------------------------------------------------- 4. 自检
echo.
echo [4/4] 环境自检...
echo.
"%VPY%" -m native_cli_orchestrator.entry --doctor
set DOCRESULT=%ERRORLEVEL%

echo.
if "%DOCRESULT%"=="0" (
  echo ==============================================================
  echo  安装完成，自检通过。
  echo.
  echo  下一步：
  echo    1^) 编辑 config\accounts.yml，填好每个账号的 env:变量名
  echo    2^) 复制 scripts\set_keys.example.bat 为 set_keys.bat，填真实凭据后执行
  echo        （或直接用 setx 设置环境变量），然后**重开终端**
  echo    3^) 把 config\config.yaml 的 mode 改成 real
  echo    4^) 启动：scripts\start_sse.bat
  echo    5^) 上层工作台添加 MCP：SSE 地址 http://127.0.0.1:8770/sse
  echo ==============================================================
) else (
  echo ==============================================================
  echo  自检**未通过**：请按上面 [ERROR] 条目的"→ 提示"逐条修好，
  echo  然后重跑：
  echo    scripts\doctor.bat
  echo ==============================================================
)
echo.
pause
exit /b 0

:no_python
echo       [失败] 找不到 python 命令
echo.
echo  请先安装 Python 3.12+：https://www.python.org/downloads/
echo  安装时务必勾选 "Add python.exe to PATH"，装完重开终端再跑本脚本。
goto :fail

:old_python
echo       [失败] Python %PYVER% 过低，需要 3.12 或更高
echo.
echo  请升级 Python：https://www.python.org/downloads/
goto :fail

:fail
echo.
pause
exit /b 1
