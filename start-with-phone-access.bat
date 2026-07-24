@echo off
REM Growth Engine — start WITH the free Cloudflare tunnel so you can open the
REM dashboard from your phone. Prints a https://...trycloudflare.com link.
REM Requires a real DASHBOARD_PASSWORD in .env (not "changeme").
setlocal
cd /d "%~dp0"
set TUNNEL_ENABLED=true
call start.bat
