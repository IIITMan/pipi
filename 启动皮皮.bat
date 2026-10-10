@echo off
title 皮皮 · 情感陪伴机器人
cd /d "%~dp0"

REM ---- 单实例保护：监控端口 8848 已被监听 = 皮皮已在运行 ----
netstat -ano | findstr ":8848 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [提示] 皮皮好像已经在运行了（端口 8848 已被占用）。
    echo.
    echo        如果找不到它的窗口，可能是进程驻留在了后台。
    echo        结束旧实例请运行:  taskkill /f /im python.exe
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 找不到 .venv 虚拟环境。
    pause
    exit /b 1
)

REM 参数透传：默认免提语音模式；想用文本模式改成 run.py 即可
".venv\Scripts\python.exe" run.py --voice %*

echo.
echo [皮皮已退出] 上方若无报错，属正常退出（Ctrl+C 或告别语句）。
echo              若有 Traceback，把最后几行截图发给助手排查。
pause
