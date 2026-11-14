@echo off
setlocal
cd /d "%~dp0"
call pi\pi_settings.bat
echo Deploying to %PI% ...
call pi\make_package.bat || goto :fail
scp -q "%PKG%" %PI%:/tmp/mm_deploy.tgz || goto :fail
ssh %PI% "bash ~/%PI_DIR%/pi/after_deploy.sh /tmp/mm_deploy.tgz"
rem The Pi's config.toml is leading (machines can be changed in the dashboard): copy it back to this PC.
if exist config.toml copy /y config.toml config.toml.before_deploy >nul
scp -q %PI%:%PI_DIR%/config.toml config.toml.from_pi && move /y config.toml.from_pi config.toml >nul && echo config.toml on this PC = the version on the Pi
pause
exit /b 0
:fail
echo !! deploy failed - is the Pi reachable? (ping %PI:*@=%)
pause
exit /b 1
