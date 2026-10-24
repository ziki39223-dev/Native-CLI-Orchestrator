@echo off
chcp 936 >nul
REM ===================================================================
REM 账号凭据设置模板（把这个文件改成 set_keys.bat 后填真实值执行）
REM
REM 说明：
REM   1. setx 写入的是「当前用户的持久环境变量」，执行后需要**重开终端**才生效
REM   2. 这里的变量名必须与 config\accounts.yml 里的 env:变量名 一致
REM   3. 真实 key 只写在这个 bat 里，不要写进 accounts.yml
REM ===================================================================

REM ---- Trae ----
setx TRAE_TOKEN_1 "在这里填 Trae 第 1 个账号的 token"
REM setx TRAE_TOKEN_2 "第 2 个账号（备用，额度耗尽后自动切）"

REM ---- Qoder ----
setx QODER_TOKEN_1 "在这里填 Qoder 第 1 个账号的 token"

REM ---- WorkBuddy ----
setx WORKBUDDY_TOKEN_1 "在这里填 WorkBuddy 第 1 个账号的 token"

REM ---- API 供应商（可选，取消注释并填写）----
REM setx DEEPSEEK_KEY_1 "sk-xxxxxxxx"

echo.
echo 环境变量已写入。请**关闭并重开终端**，然后运行：
echo   python -m native_cli_orchestrator.entry --doctor
