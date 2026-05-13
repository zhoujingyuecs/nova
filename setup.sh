#!/usr/bin/env bash
# nova 一键部署脚本（Linux / macOS）
#
# 用法：
#   ./setup.sh              # 装基础依赖 + 起 launcher
#   ./setup.sh --local      # 同时装 llama-cpp-python（本地 GGUF 用）
#   ./setup.sh --no-launch  # 装完不自动起 launcher
#
# 装完后日常使用：
#   source .venv/bin/activate
#   python launcher.py

set -e

# 颜色
BOLD="$(printf '\033[1m')"
DIM="$(printf '\033[2m')"
CYAN="$(printf '\033[36m')"
GREEN="$(printf '\033[92m')"
YELLOW="$(printf '\033[33m')"
RED="$(printf '\033[31m')"
RESET="$(printf '\033[0m')"

INSTALL_LOCAL=0
SKIP_LAUNCH=0
for arg in "$@"; do
  case "$arg" in
    --local)      INSTALL_LOCAL=1 ;;
    --no-launch)  SKIP_LAUNCH=1 ;;
    --help|-h)
      sed -n '1,12p' "$0"
      exit 0
      ;;
  esac
done

cd "$(dirname "$0")"
echo
echo "${BOLD}${CYAN}╔══════════════════════════════════════════════╗"
echo "║                  nova setup                  ║"
echo "║      陶土球 · 水流 · 一个活着的意识实验         ║"
echo "╚══════════════════════════════════════════════╝${RESET}"
echo

# ----------------------------------------------------------
# 1) 找 python
# ----------------------------------------------------------
PY=""
for cand in python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    major="${ver%%.*}"
    minor="${ver##*.}"
    if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
      PY="$cand"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "${RED}❌ 没找到 Python 3.9+ ——先装一个再回来。${RESET}"
  exit 1
fi
echo "${GREEN}✓${RESET} Python: $($PY -V 2>&1)"

# ----------------------------------------------------------
# 2) 建 venv
# ----------------------------------------------------------
if [ ! -d ".venv" ]; then
  echo "${CYAN}→${RESET} 创建虚拟环境 .venv"
  "$PY" -m venv .venv
fi
# 激活
# shellcheck disable=SC1091
. .venv/bin/activate
echo "${GREEN}✓${RESET} 已激活 .venv"

# ----------------------------------------------------------
# 3) 装基础依赖
# ----------------------------------------------------------
echo "${CYAN}→${RESET} 升级 pip"
python -m pip install --upgrade pip >/dev/null

echo "${CYAN}→${RESET} 装基础依赖（requirements.txt）"
python -m pip install -r requirements.txt
echo "${GREEN}✓${RESET} 基础依赖已装"

# ----------------------------------------------------------
# 4) 可选：装本地 LLM 依赖
# ----------------------------------------------------------
if [ "$INSTALL_LOCAL" = "1" ]; then
  echo "${CYAN}→${RESET} 装本地 LLM 依赖（llama-cpp-python）"
  echo "${DIM}   注：CUDA / Metal 加速需要按你的 GPU 重新编译。详见 README。${RESET}"
  python -m pip install -r requirements-local.txt
  echo "${GREEN}✓${RESET} 本地 LLM 依赖已装"
else
  echo "${DIM}   提示：要跑本地 GGUF？再跑一次 ./setup.sh --local${RESET}"
fi

# ----------------------------------------------------------
# 5) .env 模板
# ----------------------------------------------------------
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo "${GREEN}✓${RESET} 已生成 .env（从 .env.example 复制）"
  fi
fi

# ----------------------------------------------------------
# 6) 起 launcher
# ----------------------------------------------------------
echo
echo "${BOLD}${GREEN}🎉 安装完成。${RESET}"
echo
echo "  日常启动方式："
echo "    ${CYAN}source .venv/bin/activate${RESET}"
echo "    ${CYAN}python launcher.py${RESET}"
echo

if [ "$SKIP_LAUNCH" = "0" ]; then
  echo "${DIM}3 秒后直接进入 launcher（按 Ctrl+C 取消）…${RESET}"
  sleep 3 || exit 0
  exec python launcher.py
fi
