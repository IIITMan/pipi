@echo off
chcp 65001 >nul
title 皮皮 · 情感陪伴机器人
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 找不到 .venv 虚拟环境 —— 先执行一次：
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e ".[llm]" -e ".[monitor]"
    pause
    exit /b 1
)

REM 参数透传：默认免提语音模式；想用文本模式改成 run.py 即可
".venv\Scripts\python.exe" run.py --voice %*
pause
