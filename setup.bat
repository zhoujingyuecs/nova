@echo off
rem nova 一键部署脚本（Windows）
rem
rem 用法：
rem   setup.bat              基础部署 + 起 launcher
rem   setup.bat --local      同时装 llama-cpp-python（本地 GGUF 用）
rem   setup.bat --no-launch  装完不自动起 launcher

setlocal enabledelayedexpansion
set "INSTALL_LOCAL=0"
set "SKIP_LAUNCH=0"
for %%a in (%*) do (
  if "%%~a"=="--local"      set "INSTALL_LOCAL=1"
  if "%%~a"=="--no-launch"  set "SKIP_LAUNCH=1"
)

pushd "%~dp0"

echo.
echo ============================================
echo                 nova setup
echo    陶土球 . 水流 . 一个活着的意识实验
echo ============================================
echo.

rem ----------------------------------------------------------
rem 1) 找 python
rem ----------------------------------------------------------
set "PY="
for %%P in (py python python3) do (
  where %%P >nul 2>&1
  if not errorlevel 1 (
    if not defined PY set "PY=%%P"
  )
)
if not defined PY (
  echo [ERROR] 找不到 Python。请先安装 Python 3.9 以上：https://www.python.org/downloads/
  popd
  exit /b 1
)

%PY% -c "import sys;exit(0 if sys.version_info>=(3,9) else 1)"
if errorlevel 1 (
  echo [ERROR] Python 版本太低，需要 3.9 以上。
  popd
  exit /b 1
)
echo [OK] Python:
%PY% -V

rem ----------------------------------------------------------
rem 2) 建 venv
rem ----------------------------------------------------------
if not exist .venv (
  echo [..] 创建虚拟环境 .venv
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] venv 创建失败
    popd
    exit /b 1
  )
)
call .venv\Scripts\activate.bat
echo [OK] 已激活 .venv

rem ----------------------------------------------------------
rem 3) 装基础依赖
rem ----------------------------------------------------------
echo [..] 升级 pip
python -m pip install --upgrade pip >nul

echo [..] 装基础依赖（requirements.txt）
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] 基础依赖装失败
  popd
  exit /b 1
)
echo [OK] 基础依赖已装

rem ----------------------------------------------------------
rem 4) 可选：装本地 LLM 依赖
rem ----------------------------------------------------------
if "%INSTALL_LOCAL%"=="1" (
  echo [..] 装本地 LLM 依赖（llama-cpp-python）
  echo      注：CUDA 加速需要单独装，详见 README。
  python -m pip install -r requirements-local.txt
  if errorlevel 1 (
    echo [WARN] 本地 LLM 依赖装失败 ^(可能要先装 Visual C++ build tools^)。
    echo        没关系——你可以先用云端后端跑起来。
  ) else (
    echo [OK] 本地 LLM 依赖已装
  )
) else (
  echo      提示：要跑本地 GGUF? 再跑一次 setup.bat --local
)

rem ----------------------------------------------------------
rem 5) .env 模板
rem ----------------------------------------------------------
if not exist .env (
  if exist .env.example (
    copy /Y .env.example .env >nul
    echo [OK] 已生成 .env ^(从 .env.example 复制^)
  )
)

rem ----------------------------------------------------------
rem 6) 起 launcher
rem ----------------------------------------------------------
echo.
echo *** 安装完成。***
echo.
echo   日常启动方式：
echo     .venv\Scripts\activate
echo     python launcher.py
echo.

if "%SKIP_LAUNCH%"=="0" (
  echo 3 秒后直接进入 launcher ^(按 Ctrl+C 取消^)…
  timeout /t 3 >nul
  python launcher.py
)

popd
endlocal
