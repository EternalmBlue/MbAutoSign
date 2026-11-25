@echo off
setlocal
"%~dp0MbAutoSign-windows-x64.exe" %*
exit /b %errorlevel%
