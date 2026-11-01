@echo off
setlocal
title Victron MPPT monitor - Cerbo GX setup
cd /d "%~dp0"

set "DRIVER=dbus-mppt-monitor"
set "REMOTE=/data/dbus-mppt-monitor"
set "SSHOPT=-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
set "PACK=tar --exclude=__pycache__ --exclude=state*.json --exclude=ui*.json -cf - %DRIVER%"
set "UNPACK=tar -xf - -C /data && chmod -R u=rwX,go=rX %REMOTE%"

if not exist "%DRIVER%\dbus-mppt-monitor.py" (
    echo.
    echo  The folder "%DRIVER%" must be next to this file.
    echo.
    pause
    exit /b 1
)

set "CERBO="
if exist cerbo-ip.txt set /p CERBO=<cerbo-ip.txt
if not defined CERBO call :askip

:menu
cls
echo.
echo  ===========================================================
echo    Victron MPPT monitor  -  Cerbo GX setup
echo    Cerbo address: %CERBO%
echo    Monitor only: nothing is ever sent to the MPPT,
echo    and DVCC can't control it while the monitor runs
echo  ===========================================================
echo.
echo    1  Edit the settings (config.ini)
echo    2  Show the VE.Direct ports and what is on each
echo    3  Watch live readings from the MPPT
echo    4  Install or update the monitor on the Cerbo
echo    5  Show the monitor log
echo.
echo    U  Make a USB-stick installer (no network needed)
echo.
echo    9  Try it with a pretend MPPT (simulation)
echo.
echo    6  Change the Cerbo address
echo    7  Fix "REMOTE HOST IDENTIFICATION HAS CHANGED"
echo    8  Uninstall the monitor from the Cerbo
echo    0  Exit
echo.
set "CHOICE="
set /p "CHOICE=  Type a number and press Enter: "
if "%CHOICE%"=="1" goto edit
if "%CHOICE%"=="2" goto scan
if "%CHOICE%"=="3" goto watch
if "%CHOICE%"=="4" goto install
if "%CHOICE%"=="5" goto log
if "%CHOICE%"=="6" goto changeip
if "%CHOICE%"=="7" goto hostkey
if "%CHOICE%"=="8" goto uninstall
if "%CHOICE%"=="9" goto sim
if /i "%CHOICE%"=="U" goto usb
if "%CHOICE%"=="0" exit /b 0
goto menu

:edit
start "" notepad "%DRIVER%\config.ini"
goto menu

:scan
call :header "VE.Direct ports on the Cerbo"
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK% && python3 -u %REMOTE%/dbus-mppt-monitor.py --scan"
goto done

:watch
call :header "Live readings from the MPPT"
echo  If the monitor isn't installed yet, this stops Victron's own driver on the
echo  MPPT's port until the Cerbo restarts (so DVCC lets go of the MPPT too).
echo  To stop: press Ctrl+C, then type N and press Enter.
echo.
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK% && python3 -u %REMOTE%/dbus-mppt-monitor.py --monitor"
goto done

:install
call :header "Installing / updating the monitor"
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK% && sh %REMOTE%/install.sh && (svc -t /service/dbus-mppt-monitor 2>/dev/null; echo Starting...; sleep 15; svstat /service/dbus-mppt-monitor; echo; tail -n 15 /var/log/dbus-mppt-monitor/current 2>/dev/null | tai64nlocal)"
echo.
echo  If you see "up (pid ...)" above, the monitor is running.
echo  Look for "Control status: monitor_only" in the log lines.
goto done

:log
call :header "Monitor log"
echo  To stop: press Ctrl+C, then type N and press Enter.
echo.
ssh -t %SSHOPT% root@%CERBO% "tail -n 40 -F /var/log/dbus-mppt-monitor/current | tai64nlocal"
goto done

:sim
call :header "Simulation: a pretend MPPT on the Cerbo screen, VRM and web page"
echo  No serial port is used at all, and a real installation keeps running.
echo  You'll be asked for the password twice. While this window is open,
echo  "Solar MPPT (SIMULATED)" appears in the Cerbo's switch pane, and its web
echo  page is at http://%CERBO%:8091
echo  To stop: press Ctrl+C (then type N and press Enter if asked).
echo.
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK%"
ssh -t %SSHOPT% root@%CERBO% "python3 -u %REMOTE%/dbus-mppt-monitor.py --simulate"
goto done

:usb
cls
echo.
echo  Making a USB-stick installer
echo  -----------------------------------------------------------
echo.
tar --exclude=__pycache__ --exclude=state*.json --exclude=ui*.json --exclude=*.tmp -czf venus-data-mppt-monitor.tar.gz %DRIVER% -C usb rc
if errorlevel 1 (
    echo  Could not make the file. Windows 10 or newer is needed for the built-in "tar".
    goto done
)
echo  Made venus-data-mppt-monitor.tar.gz in this folder, with the settings from config.ini (option 1).
echo.
echo  1. Copy venus-data-mppt-monitor.tar.gz to the top folder of a USB stick or SD card
echo     (FAT32 is the safe choice). Not inside another folder.
echo  2. Put it in the Cerbo, then restart the Cerbo
echo     (Settings ^> General ^> Reboot, or switch it off and on).
echo  3. Wait about 2 minutes. The Cerbo renames the file on the stick to
echo     ...tar.gz.installed so it never installs twice. Then take the stick out.
echo  4. Open the switch pane on the Cerbo screen: the Solar MPPT cards should be there.
echo.
echo  To change settings later: option 1, then U again (a fresh file), and repeat 1 to 3.
goto done

:changeip
call :askip
goto menu

:hostkey
ssh-keygen -R %CERBO%
echo.
echo  Done. The next connection will ask nothing and remember the Cerbo again.
goto done

:uninstall
echo.
set "SURE="
set /p "SURE=  Type YES to remove the monitor from the Cerbo: "
if /i not "%SURE%"=="YES" goto menu
call :header "Uninstalling"
ssh %SSHOPT% root@%CERBO% "sh %REMOTE%/uninstall.sh; rm -rf %REMOTE%"
echo.
echo  Restart the Cerbo to give the VE.Direct port back to Victron's driver.
goto done

:done
echo.
echo  -----------------------------------------------------------
pause
goto menu

:header
cls
echo.
echo  %~1
echo  -----------------------------------------------------------
echo  When asked for a password, type the Cerbo's root password and
echo  press Enter. Nothing appears on screen while you type - that's normal.
echo.
exit /b

:askip
echo.
echo  Find the Cerbo's IP address on its screen:
echo  Settings ^> Connectivity ^> Ethernet (or Wi-Fi) ^> IP address
echo.
set "CERBO="
echo  (Installing from a USB stick instead? Just press Enter.)
set /p "CERBO=  Type the IP address (for example 192.168.1.50) and press Enter: "
if not defined CERBO (set "CERBO=not set - use option 6" & exit /b)
> cerbo-ip.txt echo %CERBO%
exit /b
