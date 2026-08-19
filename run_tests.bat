@echo off
chcp 65001 >nul 2>&1
setlocal
REM ============================================================
REM  run_tests.bat — alpha-swe 本地一键跑测（双击即用）
REM  内部定位 Git Bash 后调用 scripts/run_tests.sh，
REM  沿用其 safe-delete 规避逻辑，删除类测试本地也能通过。
REM
REM  用法：
REM    run_tests.bat                         跑全部离线用例
REM    run_tests.bat tests/test_x.py         跑指定文件
REM    run_tests.bat tests/test_x.py::test_y 跑单用例
REM ============================================================

set "REPO=%~dp0"
set "SCRIPT=%REPO%scripts\run_tests.sh"

REM 优先用 PATH 里的 bash（WorkBuddy 自带 PortableGit / 已装 Git for Windows）
set "BASH_EXE="
where bash >nul 2>&1 && set "BASH_EXE=bash"

REM 回退到常见安装路径
if not defined BASH_EXE (
  if exist "C:\Program Files\Git\bin\bash.exe" set "BASH_EXE=C:\Program Files\Git\bin\bash.exe"
)
if not defined BASH_EXE (
  if exist "C:\Program Files (x86)\Git\bin\bash.exe" set "BASH_EXE=C:\Program Files (x86)\Git\bin\bash.exe"
)
if not defined BASH_EXE (
  echo [run_tests] ERROR: 找不到 Git Bash，请安装 Git for Windows 后重试。
  pause
  exit /b 1
)

"%BASH_EXE%" "%SCRIPT%" %*

REM 无参数双击运行时，跑完暂停以便查看结果
if "%~1"=="" pause
endlocal
