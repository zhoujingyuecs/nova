# coding=UTF-8
"""
vm_agent.py —— nova 在虚拟机里的"那只手"

跑在虚拟机（192.168.122.102）上，监听 7100 端口，等 nova 通过 HTTP 让它做事。
nova 自己只在另一台机器上有缝隙、有水流、有想法；具体动作（敲命令、跑脚本、
开网页）都交给这只手。

接口（全部需要 Authorization: Bearer <token>）：
  GET  /status        —— 心跳
  POST /shell         —— 跑一条 bash 命令；cd 会持久保留工作目录
  POST /python        —— 跑一段 python；全局变量在多次调用之间保留
  POST /web           —— 抓一个 URL，HTML 会被简单粗暴地拍成纯文本

启动：
  pip install flask
  export NOVA_VM_TOKEN="改成你自己的密钥"
  python vm_agent.py
"""

import html as html_module
import io
import os
import re
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request

app = Flask(__name__)

# ============================================================
# 配置
# ============================================================
TOKEN = os.environ.get("NOVA_VM_TOKEN", "nova-vm-secret-please-change-me")
PORT = int(os.environ.get("NOVA_VM_PORT", 7100))

WORKDIR = os.path.expanduser(os.environ.get("NOVA_VM_WORKDIR", "~/nova_vm_workspace"))
os.makedirs(WORKDIR, exist_ok=True)

# 持久状态：shell 的工作目录 + python 的全局变量空间
CWD = WORKDIR
PYTHON_GLOBALS: dict = {"__name__": "__nova_vm__"}

MAX_OUTPUT_CHARS = 8000          # 单次回执文本上限
DEFAULT_SHELL_TIMEOUT = 30
WEB_BYTES_LIMIT = 2 * 1024 * 1024  # 单页最多 2MB


# ============================================================
# 工具
# ============================================================
def _truncate(text: str) -> str:
    if not text:
        return text or ""
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n…（截断，原文共 {len(text)} 字）"
    return text


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<style[^>]*>.*?</style>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    raw = re.sub(r"</(p|div|h[1-6]|li|tr|article|section)>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<br[^>]*>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html_module.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


# ============================================================
# 中间件：鉴权
# ============================================================
@app.before_request
def _auth():
    if request.path == "/status" and request.method == "GET":
        # 心跳也要鉴权，防止扫描
        pass
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401
    if auth_header[7:].strip() != TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return None


# ============================================================
# 接口
# ============================================================
@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "alive",
        "cwd": CWD,
        "python_vars": [k for k in PYTHON_GLOBALS.keys() if not k.startswith("__")],
    })


@app.route("/shell", methods=["POST"])
def shell():
    """跑一条 bash 命令。`cd <path>` 会更新持久 CWD。"""
    global CWD
    data = request.get_json(force=True, silent=True) or {}
    command = (data.get("command") or "").strip()
    timeout = int(data.get("timeout", DEFAULT_SHELL_TIMEOUT))

    if not command:
        return jsonify({"error": "empty command", "cwd": CWD})

    # 单独处理 cd —— subprocess 跑完 cd 就退出了，需要我们自己记住
    stripped = command.strip()
    if stripped == "cd" or stripped.startswith("cd ") or stripped.startswith("cd\t"):
        parts = stripped.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else os.path.expanduser("~")
        # 去掉可能的引号
        if (target.startswith('"') and target.endswith('"')) or \
           (target.startswith("'") and target.endswith("'")):
            target = target[1:-1]
        target = os.path.expanduser(target)
        if not os.path.isabs(target):
            target = os.path.join(CWD, target)
        target = os.path.abspath(target)
        if os.path.isdir(target):
            CWD = target
            return jsonify({"stdout": "", "stderr": "", "returncode": 0, "cwd": CWD})
        return jsonify({
            "stdout": "",
            "stderr": f"cd: {target}: 没有这个目录",
            "returncode": 1,
            "cwd": CWD,
        })

    try:
        print('bash --- ', CWD, command)
        result = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=CWD,
        )
        return jsonify({
            "stdout": _truncate(result.stdout),
            "stderr": _truncate(result.stderr),
            "returncode": result.returncode,
            "cwd": CWD,
        })
    except subprocess.TimeoutExpired as e:
        return jsonify({
            "error": f"超时 (>{timeout}s)",
            "stdout": _truncate(e.stdout.decode("utf-8", "ignore") if e.stdout else ""),
            "stderr": _truncate(e.stderr.decode("utf-8", "ignore") if e.stderr else ""),
            "cwd": CWD,
        })
    except Exception as e:
        return jsonify({"error": str(e), "cwd": CWD})


@app.route("/python", methods=["POST"])
def python_exec():
    """跑一段 python。变量持久保留到下一次。"""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code") or ""
    if not code.strip():
        return jsonify({"error": "empty code"})

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_buf, stderr_buf

    error = None
    old_cwd = os.getcwd()
    try:
        os.chdir(CWD)
        # 先尝试当作表达式执行（这样最后一行能自动打印结果）
        try:
            print('python --- ', CWD, code)
            compiled = compile(code, "<nova-python>", "eval")
            value = eval(compiled, PYTHON_GLOBALS)
            if value is not None:
                print(repr(value))
        except SyntaxError:
            # 不是表达式，按语句块跑
            compiled = compile(code, "<nova-python>", "exec")
            exec(compiled, PYTHON_GLOBALS)
    except Exception:
        error = traceback.format_exc()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        try:
            os.chdir(old_cwd)
        except Exception:
            pass

    return jsonify({
        "stdout": _truncate(stdout_buf.getvalue()),
        "stderr": _truncate(stderr_buf.getvalue()),
        "error": _truncate(error) if error else None,
    })


@app.route("/web", methods=["POST"])
def web():
    """抓 URL 内容。HTML 会被简化为纯文本。"""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    max_chars = int(data.get("max_chars", MAX_OUTPUT_CHARS))

    if not url:
        return jsonify({"error": "empty url"})
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    try:
        print('web --- ', url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) nova-vm/0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw_bytes = resp.read(WEB_BYTES_LIMIT)

        # 字符集
        charset = "utf-8"
        m = re.search(r"charset=([\w\-]+)", content_type, re.IGNORECASE)
        if m:
            charset = m.group(1)
        text = raw_bytes.decode(charset, errors="ignore")

        if "html" in content_type.lower() or "<html" in text[:1024].lower():
            text = _html_to_text(text)

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…（截断，原文共 {len(text)} 字）"

        return jsonify({
            "text": text,
            "url": url,
            "content_type": content_type,
        })
    except Exception as e:
        return jsonify({"error": str(e), "url": url})


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print(f"🛏️  nova 的手 醒了。监听 0.0.0.0:{PORT}")
    print(f"   工作目录：{CWD}")
    print(f"   token：   {TOKEN}")
    print(f"   （改 token 用环境变量 NOVA_VM_TOKEN）")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
