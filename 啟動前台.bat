@echo off
chcp 65001 >nul
cd /d %~dp0
echo brand-digest 前台啟動中… 這個視窗請保持開著，關掉就會停止服務。
start "" http://localhost:8765
:loop
python web.py
echo.
echo [伺服器停止] 2 秒後自動重啟（要完全結束請直接關閉此視窗）…
timeout /t 2 >nul
goto loop
