@echo off
rem REBYPRODUCT 재고관리 - 다른 PC 접속 허용 (방화벽 인바운드 규칙, 최초 1회)
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  이 파일은 [마우스 오른쪽 클릭 - 관리자 권한으로 실행] 해야 합니다.
  echo  아무 키나 누르면 닫힙니다...
  pause >nul
  exit /b
)
netsh advfirewall firewall delete rule name="REBYPRODUCT 재고관리" >nul 2>&1
netsh advfirewall firewall add rule name="REBYPRODUCT 재고관리" dir=in action=allow protocol=TCP localport=8600 profile=private,domain
echo.
echo  방화벽 허용 완료!
echo  재고관리.exe 실행 후, 다른 PC 브라우저에서  http://이PC의IP:8600  으로 접속하세요.
echo  (이 PC의 IP는 재고관리 실행 창에 표시됩니다)
echo.
echo  아무 키나 누르면 닫힙니다...
pause >nul
