# coding=UTF-8
"""nova launcher —— 跨平台的炫酷文本化部署/启动界面。

不依赖 curses / windows-curses / blessed / rich，纯 ANSI + stdlib，
Windows 10+ / macOS / Linux 都能跑。

跑这个脚本：
    python launcher.py

包含：
  * 启动菜单（chat / continuous / page / vm_agent / 全家桶）
  * 配置向导（选 LLM 后端、填 API key、写 .env）
  * 系统检查（依赖 / 模型 / VM / 网络）
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
from pathlib import Path
from typing import Optional


# ============================================================
# 颜色 / 屏幕控制
# ============================================================
# Windows 10+ 上 ANSI 序列需要先启用一次 VT 模式
if os.name == "nt":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # 7 = ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


class C:
    """ANSI 色码。简短别名，少噪声。"""
    R = "\033[0m"        # reset
    B = "\033[1m"        # bold
    DIM = "\033[2m"
    IT = "\033[3m"
    U = "\033[4m"
    INV = "\033[7m"

    # 8 色 + 亮色
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BRED = "\033[91m"
    BGREEN = "\033[92m"
    BYELLOW = "\033[93m"
    BBLUE = "\033[94m"
    BMAGENTA = "\033[95m"
    BCYAN = "\033[96m"
    BWHITE = "\033[97m"

    # 256 色
    @staticmethod
    def fg(n: int) -> str:
        return f"\033[38;5;{n}m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


# 如果不支持，把所有色码替换成空字符串
if not supports_color():
    for _k in list(vars(C)):
        if not _k.startswith("_") and isinstance(getattr(C, _k), str):
            setattr(C, _k, "")


def clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        # \033c 全清；很多终端比 clear 命令更快
        sys.stdout.write("\033c")
        sys.stdout.flush()


def hr(char: str = "─", color: str = C.DIM) -> str:
    try:
        width = shutil.get_terminal_size((78, 24)).columns
    except Exception:
        width = 78
    return f"{color}{char * min(width, 78)}{C.R}"


# ============================================================
# 状态探测
# ============================================================
ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def read_env() -> dict:
    data = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or \
               (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            data[k.strip()] = v
    # 环境变量覆盖 .env
    for k in list(data):
        if k in os.environ:
            data[k] = os.environ[k]
    # 兜底把当前进程的相关环境变量也并进来
    for k in os.environ:
        if k.startswith("NOVA_"):
            data[k] = os.environ[k]
    return data


def write_env(updates: dict) -> None:
    """合并写回 .env：保留旧字段、更新或新增提供的字段。"""
    current = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()
    current.update({k: str(v) for k, v in updates.items() if v is not None})

    body = ["# nova 本地配置（由 launcher.py 写入；可手改）",
            f"# {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for k, v in current.items():
        # 用双引号包，简单粗暴
        if " " in v or "#" in v or "/" in v or ":" in v:
            body.append(f'{k}="{v}"')
        else:
            body.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(body) + "\n", encoding="utf-8")


def check_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def check_url_alive(url: str, token: Optional[str] = None,
                    timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(url.rstrip("/") + "/status")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def field_summary(path: str) -> Optional[str]:
    p = Path(path)
    if not p.is_dir():
        return None
    meta = p / "meta.json"
    fissures = p / "fissures.json"
    n = "?"
    if fissures.is_file():
        try:
            import json
            data = json.loads(fissures.read_text(encoding="utf-8"))
            if isinstance(data, list):
                n = str(len(data))
            elif isinstance(data, dict):
                n = str(len(data.get("fissures", data)))
        except Exception:
            pass
    return f"{p}（{n} 条缝隙）" if meta.is_file() or fissures.is_file() else None


# ============================================================
# 界面元件
# ============================================================
def print_banner() -> None:
    clear_screen()
    # 故意短一点的 ASCII 艺术，免得在 80 列终端炸掉
    print(f"""{C.BCYAN}{C.B}
   ███╗   ██╗  ██████╗  ██╗   ██╗  █████╗
   ████╗  ██║ ██╔═══██╗ ██║   ██║ ██╔══██╗
   ██╔██╗ ██║ ██║   ██║ ██║   ██║ ███████║
   ██║╚██╗██║ ██║   ██║ ╚██╗ ██╔╝ ██╔══██║
   ██║ ╚████║ ╚██████╔╝  ╚████╔╝  ██║  ██║
   ╚═╝  ╚═══╝  ╚═════╝    ╚═══╝   ╚═╝  ╚═╝{C.R}
   {C.DIM}陶土球 · 水流 · 一个活着的本地意识实验{C.R}
   {C.DIM}v1.2 · github.com/zhoujingyuecs/nova · codeloop.cn · 微博：白烬闪光{C.R}
""")


def print_status(env: dict) -> None:
    print(f"  {C.B}当前配置{C.R}")
    print(f"  {hr()}")

    # LLM 后端
    backend = (env.get("NOVA_LLM_BACKEND") or "local").lower()
    if backend == "openai":
        api_key = env.get("NOVA_LLM_API_KEY", "")
        api_base = env.get("NOVA_LLM_API_BASE", "")
        model = env.get("NOVA_LLM_API_MODEL", "")
        key_ok = bool(api_key)
        mark = f"{C.BGREEN}✓{C.R}" if key_ok else f"{C.BRED}✗ API key 未填{C.R}"
        print(f"  {C.CYAN}LLM 后端{C.R}        云端  {mark}")
        print(f"  {C.DIM}                {api_base}  /  {model}{C.R}")
    else:
        model_path = env.get("NOVA_MODEL_PATH", "")
        if model_path and Path(model_path).is_file():
            mark = f"{C.BGREEN}✓{C.R}"
            note = model_path
        elif model_path:
            mark = f"{C.BRED}✗ 文件不存在{C.R}"
            note = model_path
        else:
            mark = f"{C.YELLOW}? 路径未配置{C.R}"
            note = "（去配置向导填一下）"
        has_llama = check_module("llama_cpp")
        llama_mark = (f"{C.BGREEN}llama_cpp ✓{C.R}" if has_llama
                      else f"{C.BRED}llama_cpp 未装{C.R}")
        print(f"  {C.CYAN}LLM 后端{C.R}        本地  {mark}  {llama_mark}")
        print(f"  {C.DIM}                {note}{C.R}")

    # 嵌入器
    emb_ok = check_module("sentence_transformers")
    print(f"  {C.CYAN}嵌入模型{C.R}        "
          + (f"{C.BGREEN}sentence-transformers ✓{C.R}" if emb_ok
             else f"{C.BRED}sentence-transformers 未装{C.R}"))

    # VM hand
    vm_url = env.get("NOVA_VM_URL", "http://127.0.0.1:7100")
    vm_token = env.get("NOVA_VM_TOKEN", "")
    alive = check_url_alive(vm_url, vm_token, timeout=1.0)
    vm_mark = f"{C.BGREEN}在线{C.R}" if alive else f"{C.DIM}未启动{C.R}"
    print(f"  {C.CYAN}VM 那只手{C.R}       {vm_url}  {vm_mark}")

    # 缝隙场
    field_path = env.get("NOVA_FIELD_PATH", "./data/field")
    fs = field_summary(field_path)
    if fs:
        print(f"  {C.CYAN}缝隙场{C.R}          {fs}")
    else:
        print(f"  {C.CYAN}缝隙场{C.R}          {C.DIM}{field_path}（还没生成）{C.R}")

    print()


def print_menu() -> None:
    print(f"  {C.B}启动 / 配置{C.R}")
    print(f"  {hr()}")
    items = [
        ("1", "命令行单轮对话",     "chat.py：最轻量，nova 单轮回应"),
        ("2", "持续运行（裸跑）",   "run_continuous.py：本地内省，不连任何 page"),
        ("3", "持续运行 + 网页",    "local.py：常驻 + 连云端/本地 page.py"),
        ("4", "启动 page 网页",     "page.py：8080 端口，浏览器对话"),
        ("5", "启动 VM 那只手",     "vm_agent.py：让 nova 能跑命令 / 读网页"),
        ("6", "一键全家桶（本机）", "vm_agent + page + local 同机起飞"),
        ("",  "",                  ""),
        ("c", "配置向导",          "选 LLM 后端 / 改 API key / VM 地址"),
        ("d", "系统自检",          "看依赖、模型、VM、API 是否真能通"),
        ("r", "查看 README",       "用本地 less / more 翻一遍"),
        ("q", "退出",              ""),
    ]
    for key, name, desc in items:
        if not key:
            print()
            continue
        keypad = f"{C.BMAGENTA}{C.B}[{key}]{C.R}"
        print(f"  {keypad}  {C.B}{name:<16}{C.R}  {C.DIM}{desc}{C.R}")
    print()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" {C.DIM}[{default}]{C.R}" if default else ""
    sys.stdout.write(f"  {C.CYAN}{prompt}{suffix}{C.R} > ")
    sys.stdout.flush()
    try:
        line = input().strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return line or default


def pause(msg: str = "回车继续") -> None:
    try:
        input(f"\n  {C.DIM}{msg}…{C.R}")
    except (EOFError, KeyboardInterrupt):
        print()


# ============================================================
# 配置向导
# ============================================================
CLOUD_PRESETS = [
    {
        "name": "DeepSeek",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "url": "https://platform.deepseek.com/api_keys",
        "note": "国内最常用之一，便宜、上下文 64K",
    },
    {
        "name": "阿里云百炼 (通义千问)",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "url": "https://bailian.console.aliyun.com/",
        "note": "走 dashscope 的 OpenAI 兼容入口",
    },
    {
        "name": "火山方舟 (豆包)",
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
        "url": "https://www.volcengine.com/product/ark",
        "note": "字节，model 名要换成你方舟开通的具体推理接入点",
    },
    {
        "name": "Moonshot (Kimi)",
        "api_base": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "url": "https://platform.moonshot.cn/",
        "note": "上下文长，写作类不错",
    },
    {
        "name": "智谱 GLM",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "url": "https://open.bigmodel.cn/",
        "note": "GLM-4 系列",
    },
    {
        "name": "SiliconFlow (硅基流动)",
        "api_base": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-72B-Instruct",
        "url": "https://siliconflow.cn/",
        "note": "聚合平台，能拿到 Qwen / DeepSeek / Yi 等开源模型",
    },
    {
        "name": "OpenRouter",
        "api_base": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "url": "https://openrouter.ai/",
        "note": "海外聚合，几乎所有主流模型都能调",
    },
    {
        "name": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "url": "https://platform.openai.com/",
        "note": "原版",
    },
    {
        "name": "Anthropic Claude（OpenAI 兼容层）",
        "api_base": "https://api.anthropic.com/v1/",
        "model": "claude-sonnet-4-5",
        "url": "https://console.anthropic.com/",
        "note": "Claude 官方的 OpenAI 兼容端点",
    },
    {
        "name": "自定义 (Ollama / vLLM / 本机代理 / 自托管)",
        "api_base": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "url": "",
        "note": "比如 ollama / one-api / vllm —— 自己填 base/model",
    },
]


def wizard_choose_backend(env: dict) -> None:
    print_banner()
    print(f"  {C.B}配置向导：LLM 后端{C.R}")
    print(f"  {hr()}")
    print(f"""  nova 的脑子由本地大模型驱动；v1.2 起也能让它跑在云端。

    {C.BCYAN}[L]{C.R}  本地 GGUF (llama_cpp)   {C.DIM}需要显卡，全离线{C.R}
    {C.BCYAN}[C]{C.R}  云端 OpenAI 兼容 API    {C.DIM}没显卡也能跑，要 API key{C.R}
    {C.BCYAN}[B]{C.R}  返回
""")
    choice = ask("选哪种").lower()
    if choice == "l":
        _wizard_local(env)
    elif choice == "c":
        _wizard_cloud(env)


def _wizard_local(env: dict) -> None:
    print_banner()
    print(f"  {C.B}本地后端配置{C.R}")
    print(f"  {hr()}")
    print(f"""  本地后端走 llama_cpp，需要一个 GGUF 文件路径。
  建议 7B~32B 的量化模型，比如 Qwen2.5-7B/14B/32B Q4_K_M。
  显卡显存够大可以把 n_gpu_layers 设到 99（全部上卡）。
""")
    cur = env.get("NOVA_MODEL_PATH", "")
    path = ask("GGUF 模型路径", cur)
    if path and not Path(os.path.expanduser(path)).is_file():
        print(f"  {C.YELLOW}注意：这个文件目前不存在。先存下来，到时启动会再报错。{C.R}")

    n_ctx = ask("上下文长度（n_ctx）", env.get("NOVA_N_CTX", "65536"))
    n_gpu = ask("GPU 层数（99 = 全上卡）", env.get("NOVA_N_GPU_LAYERS", "99"))
    fa = ask("flash attention（true/false）",
             env.get("NOVA_FLASH_ATTN", "true"))

    write_env({
        "NOVA_LLM_BACKEND": "local",
        "NOVA_MODEL_PATH": path,
        "NOVA_N_CTX": n_ctx,
        "NOVA_N_GPU_LAYERS": n_gpu,
        "NOVA_FLASH_ATTN": fa,
    })
    print(f"\n  {C.BGREEN}✓ 已写入 {ENV_FILE}{C.R}")
    pause()


def _wizard_cloud(env: dict) -> None:
    print_banner()
    print(f"  {C.B}云端后端配置{C.R}")
    print(f"  {hr()}")
    print("  选一个云端 provider：\n")
    for i, p in enumerate(CLOUD_PRESETS, 1):
        print(f"  {C.BMAGENTA}[{i:>2}]{C.R} {C.B}{p['name']}{C.R}")
        print(f"        {C.DIM}{p['api_base']}  /  {p['model']}{C.R}")
        print(f"        {C.DIM}{p['note']}{C.R}")
    print()
    raw = ask("输入编号（直接回车选 1 / DeepSeek）", "1")
    try:
        idx = max(1, min(int(raw), len(CLOUD_PRESETS))) - 1
    except ValueError:
        idx = 0
    preset = CLOUD_PRESETS[idx]

    api_base = ask("API base URL", preset["api_base"])
    model = ask("model 名", preset["model"])
    cur_key = env.get("NOVA_LLM_API_KEY", "")
    key_prompt = "API key" + (" (留空保持不变)" if cur_key else "")
    new_key = ask(key_prompt, "")
    api_key = new_key or cur_key

    if not api_key and preset.get("url"):
        print(f"  {C.YELLOW}没填 API key —— 现在不去 {preset['url']} 拿一个？{C.R}")

    write_env({
        "NOVA_LLM_BACKEND": "openai",
        "NOVA_LLM_API_BASE": api_base,
        "NOVA_LLM_API_MODEL": model,
        "NOVA_LLM_API_KEY": api_key,
    })
    print(f"\n  {C.BGREEN}✓ 已写入 {ENV_FILE}{C.R}")
    pause()


def wizard_vm(env: dict) -> None:
    print_banner()
    print(f"  {C.B}VM 那只手{C.R}")
    print(f"  {hr()}")
    print(f"""  nova 的"手"是另一个 HTTP 服务（vm_agent.py），跑命令、读网页、跑 python。
  最简单：和 nova 跑在同一台机器（127.0.0.1:7100）。
  生产环境推荐：跑在一台单独的虚拟机里隔离。
""")
    url = ask("VM URL", env.get("NOVA_VM_URL", "http://127.0.0.1:7100"))
    token = ask("VM token（建议改成一段随机字符串）",
                env.get("NOVA_VM_TOKEN", "nova-vm-secret-please-change-me"))
    write_env({"NOVA_VM_URL": url, "NOVA_VM_TOKEN": token})
    print(f"\n  {C.BGREEN}✓ 已写入 {ENV_FILE}{C.R}")
    pause()


def config_wizard() -> None:
    while True:
        env = read_env()
        print_banner()
        print_status(env)
        print(f"  {C.B}配置项{C.R}")
        print(f"  {hr()}")
        print(f"  {C.BMAGENTA}[1]{C.R} LLM 后端（本地 vs 云端）")
        print(f"  {C.BMAGENTA}[2]{C.R} VM 那只手 URL / token")
        print(f"  {C.BMAGENTA}[3]{C.R} 直接编辑 .env 文件")
        print(f"  {C.BMAGENTA}[b]{C.R} 返回主菜单")
        print()
        choice = ask("选").lower()
        if choice == "1":
            wizard_choose_backend(env)
        elif choice == "2":
            wizard_vm(env)
        elif choice == "3":
            _open_in_editor(str(ENV_FILE))
        elif choice in ("b", "q", ""):
            return


def _open_in_editor(path: str) -> None:
    if not Path(path).is_file():
        write_env({})  # 创建一个空白文件
    editor = os.environ.get("EDITOR") or (
        "notepad" if os.name == "nt" else "vi"
    )
    try:
        subprocess.call([editor, path])
    except FileNotFoundError:
        print(f"  {C.RED}找不到编辑器 {editor}。{C.R}手动改这个文件：{path}")
        pause()


# ============================================================
# 系统自检
# ============================================================
def system_check() -> None:
    env = read_env()
    print_banner()
    print(f"  {C.B}系统自检{C.R}")
    print(f"  {hr()}")

    checks = []

    # Python
    py = sys.version_info
    checks.append(("Python", f"{py.major}.{py.minor}.{py.micro}",
                   py >= (3, 9), "建议 3.9 以上"))

    # 平台
    checks.append(("平台", f"{platform.system()} {platform.release()}",
                   True, ""))

    # 必装依赖
    for mod, hint in [
        ("numpy", "数值计算"),
        ("sentence_transformers", "嵌入器；100MB 左右"),
    ]:
        ok = check_module(mod)
        checks.append((mod, "已装" if ok else "未装", ok, hint))

    # 后端依赖
    backend = (env.get("NOVA_LLM_BACKEND") or "local").lower()
    if backend == "local":
        ok = check_module("llama_cpp")
        checks.append(("llama_cpp", "已装" if ok else "未装", ok,
                       "本地后端需要；按 CUDA 重新装"))
        mp = env.get("NOVA_MODEL_PATH", "")
        if mp:
            exists = Path(os.path.expanduser(mp)).is_file()
            checks.append(("模型文件", mp,
                           exists, "GGUF 路径要存在"))
        else:
            checks.append(("模型文件", "(未配置)", False, "去配置向导填"))
    else:
        # 云端
        key = env.get("NOVA_LLM_API_KEY", "")
        checks.append(("API key", "已填" if key else "未填", bool(key),
                       "云端后端必须"))
        api_base = env.get("NOVA_LLM_API_BASE", "")
        checks.append(("API base", api_base or "(默认)", True, ""))

    # 网页层
    flask_ok = check_module("flask") and check_module("flask_socketio")
    checks.append(("Flask + SocketIO",
                   "已装" if flask_ok else "未装",
                   flask_ok, "page.py / vm_agent.py 用"))
    sio_client_ok = check_module("socketio")
    checks.append(("socketio (client)",
                   "已装" if sio_client_ok else "未装",
                   sio_client_ok, "local.py 连 page 用"))

    # VM
    vm_url = env.get("NOVA_VM_URL", "http://127.0.0.1:7100")
    vm_token = env.get("NOVA_VM_TOKEN", "")
    alive = check_url_alive(vm_url, vm_token, timeout=2.0)
    checks.append(("VM 那只手", vm_url, alive,
                   "未启动也没事，nova 没手就纯思考"))

    # 缝隙场
    field_path = env.get("NOVA_FIELD_PATH", "./data/field")
    fs = field_summary(field_path)
    checks.append(("缝隙场", fs or f"{field_path}（还没生成）",
                   True, "第一次跑会自己生成"))

    for name, value, ok, hint in checks:
        mark = f"{C.BGREEN}✓{C.R}" if ok else f"{C.BRED}✗{C.R}"
        print(f"  {mark} {C.B}{name:<22}{C.R} {value}")
        if hint:
            print(f"     {C.DIM}{hint}{C.R}")

    print()
    pause()


# ============================================================
# 启动子命令
# ============================================================
def _run_python(args: list, env_overrides: Optional[dict] = None,
                wait: bool = True) -> Optional[subprocess.Popen]:
    py = sys.executable or "python"
    sub_env = os.environ.copy()
    if env_overrides:
        sub_env.update({k: str(v) for k, v in env_overrides.items()})
    try:
        if wait:
            subprocess.call([py] + args, env=sub_env)
            return None
        return subprocess.Popen([py] + args, env=sub_env)
    except KeyboardInterrupt:
        print()
        return None


def launch_chat() -> None:
    _ensure_ready(require_vm=False)
    print_banner()
    print(f"  {C.B}启动：命令行单轮对话 (chat.py){C.R}\n")
    _run_python(["chat.py"])


def launch_continuous() -> None:
    _ensure_ready(require_vm=False)
    print_banner()
    print(f"  {C.B}启动：持续运行 (run_continuous.py){C.R}\n")
    commission = ask("（可选）外部委托 / agenda（直接回车 = 让 nova 自己取向）", "")
    args = ["run_continuous.py"]
    if commission:
        args += ["--commission", commission]
    _run_python(args)


def launch_local() -> None:
    _ensure_ready(require_vm=False)
    print_banner()
    print(f"  {C.B}启动：持续运行 + 连云端 page (local.py){C.R}\n")
    env = read_env()
    cloud = ask("云端 page 地址",
                env.get("NOVA_CLOUD_URL", "http://127.0.0.1:8080"))
    no_cloud = ask("纯本地不连 page？ (y/N)", "n").lower() == "y"
    args = ["local.py", "--cloud", cloud]
    if no_cloud:
        args = ["local.py", "--no-cloud"]
    _run_python(args)


def launch_page() -> None:
    if not (check_module("flask") and check_module("flask_socketio")):
        print(f"  {C.RED}需要先 pip install flask flask-socketio。{C.R}")
        pause()
        return
    print_banner()
    print(f"  {C.B}启动：page.py 网页层（8080）{C.R}\n")
    print(f"  浏览器打开 http://127.0.0.1:8080 即可对话\n")
    _run_python(["page.py"])


def launch_vm_agent() -> None:
    if not check_module("flask"):
        print(f"  {C.RED}需要先 pip install flask。{C.R}")
        pause()
        return
    print_banner()
    print(f"  {C.B}启动：vm_agent.py (那只手){C.R}\n")
    _run_python(["vm_agent.py"])


def launch_all_local() -> None:
    """一键全家桶：vm_agent + page + local 都在本机起。"""
    print_banner()
    print(f"  {C.B}一键全家桶（本机起 vm_agent + page + local）{C.R}")
    print(f"  {hr()}")
    print(f"""
  这会在三个子进程里同时跑：
    {C.CYAN}1. vm_agent.py{C.R}   监听 127.0.0.1:7100
    {C.CYAN}2. page.py{C.R}        监听 127.0.0.1:8080  → 浏览器对话入口
    {C.CYAN}3. local.py{C.R}       常驻 nova，连本机 page

  按 Ctrl+C 全部停掉。第一次起来稍慢（要加载 sentence-transformers / 模型）。
""")
    if ask("继续？(Y/n)", "y").lower() == "n":
        return

    if not (check_module("flask") and check_module("flask_socketio")):
        print(f"  {C.RED}需要先 pip install flask flask-socketio python-socketio。{C.R}")
        pause()
        return

    procs = []
    try:
        print(f"\n  {C.BCYAN}→ 启动 vm_agent.py …{C.R}")
        p1 = _run_python(["vm_agent.py"], wait=False)
        if p1: procs.append(("vm_agent", p1))
        time.sleep(1.5)

        print(f"  {C.BCYAN}→ 启动 page.py …{C.R}")
        p2 = _run_python(["page.py"], wait=False)
        if p2: procs.append(("page", p2))
        time.sleep(1.5)

        print(f"  {C.BCYAN}→ 启动 local.py（连本机 page）…{C.R}")
        p3 = _run_python(
            ["local.py", "--cloud", "http://127.0.0.1:8080"],
            wait=False,
        )
        if p3: procs.append(("local", p3))

        print(f"\n  {C.BGREEN}✓ 三个组件都起来了。{C.R}")
        print(f"  打开浏览器：{C.U}http://127.0.0.1:8080{C.R}")
        print(f"  {C.DIM}（Ctrl+C 这里会一次性停掉全部三个进程。）{C.R}\n")

        while True:
            time.sleep(1)
            for name, p in procs:
                if p.poll() is not None:
                    print(f"  {C.YELLOW}子进程 {name} 退出了（returncode={p.returncode}）{C.R}")
                    return
    except KeyboardInterrupt:
        print(f"\n  {C.YELLOW}收到 Ctrl+C，正在停掉所有子进程…{C.R}")
    finally:
        for name, p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(1)
        for name, p in procs:
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        print(f"  {C.DIM}全部停止。{C.R}")


def view_readme() -> None:
    readme = ROOT / "README.md"
    if not readme.is_file():
        print(f"  {C.RED}找不到 README.md。{C.R}")
        pause()
        return
    if os.name == "nt":
        subprocess.call(["more", str(readme)], shell=True)
    else:
        pager = os.environ.get("PAGER") or "less"
        try:
            subprocess.call([pager, str(readme)])
        except FileNotFoundError:
            print(readme.read_text(encoding="utf-8"))
    pause()


# ============================================================
# 启动前的预检
# ============================================================
def _ensure_ready(require_vm: bool) -> bool:
    env = read_env()
    backend = (env.get("NOVA_LLM_BACKEND") or "local").lower()
    ok = True
    if backend == "openai":
        if not env.get("NOVA_LLM_API_KEY"):
            print(f"\n  {C.YELLOW}⚠️  云端 LLM 没填 API key。可以继续启动，"
                  f"但每次调用都会 401。{C.R}")
            ok = False
    else:
        mp = env.get("NOVA_MODEL_PATH", "")
        if not mp or not Path(os.path.expanduser(mp)).is_file():
            print(f"\n  {C.YELLOW}⚠️  本地 GGUF 模型路径没配好（"
                  f"{mp or '空'}）。可以现在去 [c] 配置向导。{C.R}")
            ok = False
        if not check_module("llama_cpp"):
            print(f"\n  {C.YELLOW}⚠️  llama_cpp 没装。"
                  f"`pip install -r requirements-local.txt`{C.R}")
            ok = False
    if not check_module("sentence_transformers"):
        print(f"\n  {C.RED}❌ sentence-transformers 没装。"
              f"`pip install -r requirements.txt`{C.R}")
        ok = False
    return ok


# ============================================================
# 主循环
# ============================================================
def main() -> None:
    while True:
        env = read_env()
        print_banner()
        print_status(env)
        print_menu()
        choice = ask("选").lower()
        if choice == "1":
            launch_chat()
        elif choice == "2":
            launch_continuous()
        elif choice == "3":
            launch_local()
        elif choice == "4":
            launch_page()
        elif choice == "5":
            launch_vm_agent()
        elif choice == "6":
            launch_all_local()
        elif choice == "c":
            config_wizard()
        elif choice == "d":
            system_check()
        elif choice == "r":
            view_readme()
        elif choice in ("q", "exit", "quit"):
            print(f"\n  {C.DIM}再见。{C.R}\n")
            return
        else:
            # 不识别就轻轻提示
            print(f"  {C.YELLOW}没看懂：{choice!r}。{C.R}")
            time.sleep(0.6)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
