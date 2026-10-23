@echo off
rem REBYPRODUCT 재고관리 - 서버 PC 방화벽 허용 (더블클릭만 하면 됩니다. 처음 한 번)
rem  · 관리자 권한이 없으면 스스로 관리자 권한으로 다시 실행합니다 (허용 창에서 [예])
rem  · 접속 포트 8600을 개인/도메인/공용 네트워크 모두 허용 (공용으로 잡힌 회사 와이파이 대비)
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  관리자 권한이 필요합니다 - 잠시 후 뜨는 허용 창에서 [예]를 눌러주세요.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
title REBYPRODUCT 방화벽 허용
echo.
echo  [1/2] 이전 규칙 정리...
netsh advfirewall firewall delete rule name="REBYPRODUCT 재고관리" >nul 2>&1
netsh advfirewall firewall delete rule name="REBYPRODUCT 재고관리 (8600)" >nul 2>&1
echo  [2/2] 8600 포트 허용 규칙 추가...
netsh advfirewall firewall add rule name="REBYPRODUCT 재고관리 (8600)" dir=in action=allow protocol=TCP localport=8600 profile=any
if errorlevel 1 (
  echo.
  echo  ** 규칙 추가에 실패했습니다. 백신 프로그램 방화벽을 쓰는 경우 그 프로그램에서 8600 포트를 허용해주세요.
  echo.
  pause
  exit /b
)
echo.
echo  ==============================================================
echo   방화벽 허용 완료!
echo   RebyStock.exe 를 켜 둔 상태에서, 다른 PC 브라우저에 아래 주소를 입력하세요.
echo  ==============================================================
for /f "usebackq tokens=*" %%a in (`powershell -NoProfile -Command "Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | ForEach-Object { $_.IPv4Address.IPAddress } | ForEach-Object { '    http://' + $_ + ':8600' }"`) do echo %%a
echo.
echo   * 같은 공유기(같은 와이파이/랜)에 연결된 PC에서만 위 주소로 들어올 수 있습니다.
echo   * 회사 밖에서는 채팅방에 올라오는 외부 접속 주소(https://...trycloudflare.com)를 쓰세요.
echo.
echo  아무 키나 누르면 닫힙니다...
pause >nul
