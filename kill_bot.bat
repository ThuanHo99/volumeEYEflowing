@echo off
echo ================================
echo  KILL BTC-AI-SIGNAL-TRACKER
echo ================================
echo.
echo [1] Dang tim process dang chay...
wmic process where "commandline like '%%btc-ai-signal-tracker%%'" get processid,commandline 2>nul
echo.
echo [2] Dang kill tat ca process...
wmic process where "commandline like '%%btc-ai-signal-tracker%%'" delete 2>nul
echo.
echo [3] Kiem tra lai...
tasklist | findstr /i python
tasklist | findstr /i node
echo.
echo ================================
echo  DA KILL XONG!
echo ================================
pause