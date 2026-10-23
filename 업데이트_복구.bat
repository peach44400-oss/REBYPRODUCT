@echo off
rem REBYPRODUCT 복구 업데이트 - 화면이 비어 업데이트 버튼을 못 누를 때, RebyStock.exe 폴더에 넣고 더블클릭
title REBYPRODUCT 복구 업데이트
cd /d "%~dp0"
if not exist "RebyStock.exe" (
  echo.
  echo  이 파일을 RebyStock.exe 가 있는 폴더에 넣고 다시 실행해주세요.
  echo  지금 폴더: %~dp0
  pause
  exit /b
)
echo.
echo  [1/4] 실행 중인 프로그램 종료...
taskkill /f /im RebyStock.exe >nul 2>&1
timeout /t 3 /nobreak >nul
echo  [2/4] 최신 버전 내려받는 중... (1~2분)
del /f /q "RebyStock_복구.exe" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing 'https://github.com/peach44400-oss/REBYPRODUCT/releases/latest/download/RebyStock.exe' -OutFile 'RebyStock_복구.exe'"
if not exist "RebyStock_복구.exe" goto fail
for %%A in ("RebyStock_복구.exe") do if %%~zA LSS 5000000 goto fail
echo  [3/4] 파일 교체...
set n=0
:retry
move /y "RebyStock_복구.exe" "RebyStock.exe" >nul 2>&1
if not errorlevel 1 goto done
set /a n+=1
if %n% GEQ 10 goto fail2
timeout /t 2 /nobreak >nul
goto retry
:done
echo  [4/4] 프로그램 다시 실행...
start "" "%~dp0RebyStock.exe"
echo.
echo  복구 완료! 잠시 후 브라우저에 로그인 창이 뜹니다.
timeout /t 5 >nul
exit /b
:fail
echo.
echo  ** 내려받기에 실패했습니다 - 인터넷 연결을 확인하고 다시 실행해주세요.
del /f /q "RebyStock_복구.exe" >nul 2>&1
pause
exit /b
:fail2
echo.
echo  ** 파일을 바꾸지 못했습니다 - PC를 재시작한 뒤 이 파일을 다시 실행해주세요.
pause
exit /b
