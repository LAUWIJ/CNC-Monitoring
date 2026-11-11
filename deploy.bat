@echo off
setlocal
cd /d "%~dp0"
call pi\pi_settings.bat
echo Deploying to %PI% ...
call pi\make_package.bat || goto :fail
scp -q "%PKG%" %PI%:/tmp/mm_deploy.tgz || goto :fail
ssh %PI% "bash ~/%PI_DIR%/pi/after_deploy.sh /tmp/mm_deploy.tgz"
pause
exit /b 0
:fail
echo !! deploy failed - is the Pi reachable? (ping %PI:*@=%)
pause
exit /b 1
