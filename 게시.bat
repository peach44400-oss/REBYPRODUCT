@echo off
chcp 65001 >nul
setlocal
title 재고관리 새 버전 게시

REM ── 새 버전 게시 스크립트 ──────────────────────────────
REM  1) app\main.py 의 APP_VERSION 을 새 버전으로 올리고
REM  2) version.json 의 "version"·"notes" 를 맞춘 뒤
REM  3) 이 배치를 실행하면: 빌드 → GitHub Release 생성 → exe·version.json 업로드
REM  * GitHub CLI(gh)가 설치·로그인돼 있어야 합니다.  gh auth login
REM ───────────────────────────────────────────────────────

REM version.json 에서 버전 읽기
for /f "usebackq tokens=2 delims=:, " %%v in (`findstr /i "\"version\"" version.json`) do set VER=%%~v
if "%VER%"=="" (
  echo [오류] version.json 에서 version 을 읽지 못했습니다.
  pause & exit /b 1
)
echo 게시할 버전: v%VER%
echo.

echo [1/3] exe 빌드 중...
python -m PyInstaller --noconfirm MartinStock.spec
if not exist "dist\재고관리.exe" (
  echo [오류] 빌드 실패 - dist\재고관리.exe 가 없습니다.
  pause & exit /b 1
)

echo.
echo [2/3] GitHub Release 생성 (v%VER%)...
gh release create "v%VER%" "dist\재고관리.exe" "version.json" --title "v%VER%" --notes-file version.json
if errorlevel 1 (
  echo [오류] Release 생성 실패. gh 로그인 상태를 확인하세요:  gh auth status
  pause & exit /b 1
)

echo.
echo [3/3] 정리...
rmdir /s /q dist build 2>nul
del MartinStock.spec.bak 2>nul

echo.
echo ============================================
echo  v%VER% 게시 완료!
echo  이제 각 PC의 프로그램에서 [관리 - 업데이트 확인]을 누르면 받아집니다.
echo ============================================
pause
