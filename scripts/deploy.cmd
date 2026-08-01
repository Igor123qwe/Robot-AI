@echo off
rem Запуск с ПК: залить изменения на GitHub и сразу обновить робота.
rem Адрес робота можно переопределить: set ROBOT_HOST=192.168.0.51
chcp 65001 >nul
setlocal

if "%ROBOT_HOST%"=="" set ROBOT_HOST=wheeltec@192.168.0.50

cd /d "%~dp0.."

echo === отправляю на GitHub
git push
if errorlevel 1 (
  echo.
  echo !! push не прошёл. Нечего отправлять? Тогда просто обновлю робота.
  echo.
)

echo.
echo === обновляю робота (%ROBOT_HOST%)
ssh %ROBOT_HOST% "bash ~/Robot-AI/scripts/update.sh"

echo.
pause
