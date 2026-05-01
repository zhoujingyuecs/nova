#!/usr/bin/env python3
"""
doubao_chat.py
==============
豆包网页版自动化工具 v4

变化（相对 v3）：
- 验证码出现时不再直接报错，而是「暂停 + 提示用户用 SSH 隧道 + chrome://inspect 手动过验证」
- chat 子命令也开远程调试端口（默认 9222），随时可以连过去
- 改进 login 的 Ctrl+C 处理（之前关闭时偶发报错，profile 通常已保存，吞掉异常即可）
- 验证码通过后自动继续轮询回复

用法：
  # 第一次：交互式登录建立 profile
  python3 doubao_chat.py login --profile-dir ./doubao-profile

  # 之后：跑 chat。即使再触发验证码，也会暂停等你手动过
  python3 doubao_chat.py chat -i input.txt -o output.txt --profile-dir ./doubao-profile
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError as PWTimeout,
)


# ---------------------------------------------------------------------------
# Stealth：减少 headless 浏览器指纹
# ---------------------------------------------------------------------------
STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined }); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] }); } catch (e) {}
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
        { name: 'Native Client', filename: 'internal-nacl-plugin' },
      ],
    });
  } catch (e) {}
  try {
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || { id: undefined };
  } catch (e) {}
  try {
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
      navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery.call(navigator.permissions, p);
    }
  } catch (e) {}
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter.call(this, p);
    };
  } catch (e) {}
})();
"""


# ---------------------------------------------------------------------------
# JS 启发式提取消息
# ---------------------------------------------------------------------------
EXTRACT_MESSAGES_JS = r"""
() => {
    const skipTags = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON', 'NOSCRIPT', 'SVG', 'PATH']);

    function normalizeText(t) {
        return String(t || '')
            .replace(/\r/g, '')
            .replace(/\u00a0/g, ' ')
            .replace(/[ \t]+/g, ' ')
            .replace(/[ \t]*\n[ \t]*/g, '\n')
            .replace(/\n{3,}/g, '\n\n')
            .trim();
    }

    function classText(el) {
        try {
            if (!el) return '';
            if (typeof el.className === 'string') return el.className;
            if (el.getAttribute) return el.getAttribute('class') || '';
        } catch (e) {}
        return '';
    }

    function isVisible(el) {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return false;
        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
        return true;
    }

    function isHiddenForText(el, root) {
        if (!el || el === root) return false;
        if (!el.getBoundingClientRect) return false;
        const cls = classText(el);
        // 豆包 markdown 换行节点通常没有文字，不能因为尺寸小就跳过。
        if (cls.includes('md-box-line-break') || cls.includes('line-break')) return false;
        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return true;
        return false;
    }

    function collectText(root) {
        const out = [];
        const blockTags = new Set(['P', 'DIV', 'SECTION', 'ARTICLE', 'LI', 'UL', 'OL', 'PRE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'TR']);

        function pushText(t) {
            if (!t) return;
            const v = String(t).replace(/\u00a0/g, ' ');
            if (!v) return;
            out.push(v);
        }

        function pushNewline() {
            while (out.length && /^[ \t]*$/.test(out[out.length - 1])) out.pop();
            if (!out.length) return;
            const last = out[out.length - 1];
            if (!String(last).endsWith('\n')) out.push('\n');
        }

        function walk(node) {
            if (!node) return;
            if (node.nodeType === Node.TEXT_NODE) {
                pushText(node.nodeValue || '');
                return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return;

            const el = node;
            const tag = el.tagName;
            const cls = classText(el);

            if (tag === 'BR' || cls.includes('md-box-line-break') || cls.includes('line-break')) {
                pushNewline();
                return;
            }
            if (skipTags.has(tag)) return;
            if (el !== root && el.closest('[data-foundation-type$="message-action-bar"]')) return;
            if (isHiddenForText(el, root)) return;

            const beforeLen = out.length;
            for (const child of Array.from(el.childNodes)) walk(child);

            let display = '';
            try { display = window.getComputedStyle(el).display || ''; } catch (e) {}
            const isBlock = blockTags.has(tag) || cls.includes('paragraph-element') || display === 'block' || display === 'list-item';
            if (isBlock && out.length > beforeLen) pushNewline();
        }

        walk(root);
        return normalizeText(out.join(''));
    }

    function textOf(root) {
        if (!root) return '';
        const inner = normalizeText(root.innerText || '');
        const collected = collectText(root);
        // innerText 通常最接近用户看到的内容；但豆包的 markdown 换行 div 在某些环境下
        // 可能不会进 innerText，这时用自定义遍历保留换行。
        if (collected && collected.includes('\n') && !inner.includes('\n')) return collected;
        return inner || collected;
    }

    function uniqRoots(roots) {
        const out = [];
        for (const r of roots) {
            if (!r) continue;
            if (out.some(x => x === r || x.contains(r))) continue;
            for (let i = out.length - 1; i >= 0; i--) {
                if (r.contains(out[i])) out.splice(i, 1);
            }
            out.push(r);
        }
        return out;
    }

    function inferRole(item) {
        if (!item) return 'unknown';
        if (item.querySelector('[data-foundation-type="receive-message-action-bar"]')) return 'receive';
        if (item.querySelector('[data-foundation-type="send-message-action-bar"]')) return 'send';
        if (item.querySelector('.flow-markdown-body')) return 'receive';
        if (item.querySelector('[class*="send-msg-bubble"], [class*="whitespace-pre-wrap"]')) return 'send';
        return 'unknown';
    }

    function contentRoots(item, role) {
        const core = (item.matches && item.matches('[data-message-id]'))
            ? item
            : (item.querySelector(':scope [data-message-id]') || item);

        let roots = [];
        if (role === 'receive') {
            roots = roots.concat(Array.from(core.querySelectorAll(':scope .flow-markdown-body')));
            roots = roots.concat(Array.from(core.querySelectorAll(':scope [data-plugin-identifier="block_type:10000"] .flow-markdown-body')));
            if (roots.length === 0) roots = roots.concat(Array.from(core.querySelectorAll(':scope [data-plugin-identifier="block_type:10000"]')));
            if (roots.length === 0) roots = roots.concat(Array.from(core.querySelectorAll(':scope [data-render-engine="node"]')));
        } else if (role === 'send') {
            roots = roots.concat(Array.from(core.querySelectorAll(':scope [class*="send-msg-bubble"], :scope [class*="whitespace-pre-wrap"]')));
            if (roots.length === 0) roots = roots.concat(Array.from(core.querySelectorAll(':scope [data-render-engine="node"]')));
        } else {
            roots = roots.concat(Array.from(core.querySelectorAll(':scope .flow-markdown-body')));
            roots = roots.concat(Array.from(core.querySelectorAll(':scope [class*="send-msg-bubble"], :scope [class*="whitespace-pre-wrap"]')));
            if (roots.length === 0) roots = [core];
        }
        return uniqRoots(roots);
    }

    function messageIdOf(item) {
        const midEl = (item.matches && item.matches('[data-message-id]'))
            ? item
            : item.querySelector('[data-message-id]');
        return (midEl && midEl.getAttribute('data-message-id'))
            || item.getAttribute('data-message-list-item-id')
            || '';
    }

    // 优先使用 success_dom 中稳定出现的豆包消息结构：
    // - 每条消息外层：data-message-list-item-id
    // - AI 回复动作栏：data-foundation-type="receive-message-action-bar"
    // - 用户消息动作栏：data-foundation-type="send-message-action-bar"
    // - AI 正文：.flow-markdown-body
    let messageItems = Array.from(document.querySelectorAll('[data-message-list-item-id]'));
    if (messageItems.length === 0) {
        messageItems = Array.from(document.querySelectorAll('[data-message-id]'));
    }

    const items = [];
    messageItems.forEach((item, index) => {
        if (!isVisible(item)) return;
        const role = inferRole(item);
        const roots = contentRoots(item, role);
        const parts = roots.map(textOf).filter(Boolean);
        let text = normalizeText(parts.join('\n\n'));
        if (!text) return;

        // 防止极少数情况下把操作栏文字拼进正文。
        text = text
            .replace(/\n?(复制|重新生成|分享|点赞|点踩|更多|朗读)\s*$/g, '')
            .trim();
        if (!text) return;

        const r = item.getBoundingClientRect();
        items.push({
            role,
            text,
            id: messageIdOf(item),
            top: r.top,
            index,
        });
    });

    if (items.length > 0) {
        // querySelectorAll 本身就是文档顺序，比 top 更不容易受虚拟列表/滚动位置影响。
        items.sort((a, b) => a.index - b.index);
        return {
            strategy: 'doubao-message-list-item',
            messages: items.map(i => i.text),
            items,
            count: items.length,
        };
    }

    // 兜底：旧版/异常 DOM 的通用选择器。
    const trySelectors = [
        '[data-testid*="receive_message" i]',
        '[data-testid*="send_message" i]',
        '[data-testid*="message" i]',
        '[class*="receive-message"]',
        '[class*="ReceiveMessage"]',
        '[class*="send-message"]',
        '[class*="SendMessage"]',
        '[class*="message-item"]',
        '[class*="MessageItem"]',
        '[class*="messageItem"]',
        '[class*="msg-item"]',
        '[class*="msgItem"]',
        '[class*="chat-item"]',
        '[class*="ChatItem"]',
        '[class*="chat-message"]',
        '[class*="ChatMessage"]',
        '[class*="msg-content"]',
        '[class*="message-content"]',
        '[class*="msg-bubble"]',
        '[class*="MsgBubble"]',
        '[role="article"]',
        '[role="listitem"]',
    ];

    for (const sel of trySelectors) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); }
        catch (e) { continue; }
        if (!nodes || nodes.length === 0) continue;

        const fallbackItems = [];
        nodes.forEach((el, index) => {
            if (!isVisible(el)) return;
            const t = textOf(el);
            if (!t) return;
            const r = el.getBoundingClientRect();
            fallbackItems.push({ role: 'unknown', text: t, id: '', top: r.top, index });
        });
        if (fallbackItems.length === 0) continue;

        fallbackItems.sort((a, b) => a.index - b.index);
        return {
            strategy: 'selector:' + sel,
            messages: fallbackItems.map(i => i.text),
            items: fallbackItems,
            count: fallbackItems.length,
        };
    }

    // 最终兜底：直接走 body 的可见文本节点。
    const all = [];
    function walk(el) {
        if (!el || skipTags.has(el.tagName)) return;
        if (!isVisible(el)) return;
        const direct = Array.from(el.childNodes)
            .filter(n => n.nodeType === Node.TEXT_NODE && (n.textContent || '').trim().length > 0)
            .map(n => n.textContent.trim()).join(' ');
        if (direct.length > 5) {
            const r = el.getBoundingClientRect();
            all.push({ role: 'unknown', text: normalizeText(direct), id: '', top: r.top, index: all.length });
        }
        for (const c of el.children) walk(c);
    }
    walk(document.body);
    return {
        strategy: 'body-walk',
        messages: all.map(a => a.text),
        items: all,
        count: all.length,
    };
}
"""


# ---------------------------------------------------------------------------
# 选择器
# ---------------------------------------------------------------------------
INPUT_SELECTORS = [
    'textarea[data-testid*="input"]',
    'textarea[placeholder*="发"]',
    'textarea[placeholder*="问"]',
    'textarea[placeholder*="输入"]',
    'textarea[placeholder*="message"]',
    'textarea[placeholder*="Message"]',
    'div[contenteditable="true"][data-testid*="input"]',
    'div[contenteditable="true"]',
    'textarea',
]

SEND_BUTTON_SELECTORS = [
    'button[data-testid*="send"]',
    'button[aria-label*="发送"]',
    'button[aria-label*="Send"]',
    'button:has-text("发送")',
]

STOP_BUTTON_SELECTORS = [
    'button[data-testid*="stop"]',
    'button[aria-label*="停止"]',
    'button[aria-label*="Stop"]',
    'button:has-text("停止生成")',
    'button:has-text("停止")',
]

CAPTCHA_INDICATORS = [
    '#captcha_container',
    'iframe[src*="rmc.bytedance.com/verifycenter"]',
    'iframe[src*="verifycenter/captcha"]',
    'iframe[src*="/captcha"]',
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    print(f"[doubao] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 检查工具
# ---------------------------------------------------------------------------
async def captcha_visible(page: Page) -> bool:
    for sel in CAPTCHA_INDICATORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=200):
                return True
        except Exception:
            pass
    return False


async def find_first_visible(page: Page, selectors, timeout_ms=5000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            return loc, sel
        except PWTimeout:
            continue
        except Exception:
            continue
    return None, None


async def is_anything_visible(page: Page, selectors) -> bool:
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=200):
                return True
        except Exception:
            pass
    return False


async def get_messages(page: Page) -> dict:
    try:
        result = await page.evaluate(EXTRACT_MESSAGES_JS)
        if isinstance(result, dict):
            return result
    except Exception as e:
        log(f"提取消息异常：{e}")
    return {"strategy": None, "messages": [], "count": 0}


def looks_like_user_msg(text: str, user_input: str) -> bool:
    if not text or not user_input:
        return False
    a = text.strip()
    b = user_input.strip()
    if a == b:
        return True
    if a.startswith(b) and len(a) - len(b) < max(20, len(b) * 0.2):
        return True
    if b in a and len(a) - len(b) < 50:
        return True
    return False


def latest_assistant_message(messages_result: dict, user_input: str, before_ids=None, before_texts=None) -> str:
    """从 get_messages() 的结果里取最新一条豆包回复。

    新版豆包 DOM 里可以区分 receive/send，因此优先只取 role=receive 的消息；
    同时用发送前已有的 message id 排除历史回复，避免发送成功但读取到上一轮回复。
    """
    before_ids = set(before_ids or [])
    before_texts = set(before_texts or [])

    items = messages_result.get("items") or []
    if items:
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if item.get("role") != "receive":
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            msg_id = str(item.get("id") or "").strip()
            if msg_id and msg_id in before_ids:
                continue
            if not msg_id and text in before_texts:
                continue
            if looks_like_user_msg(text, user_input):
                continue
            return text

    # 兼容旧版 fallback：没有 role/id 时，仍按原逻辑从后往前找非用户消息。
    for text in reversed(messages_result.get("messages") or []):
        text = (text or "").strip()
        if not text:
            continue
        if looks_like_user_msg(text, user_input):
            continue
        if text in before_texts:
            continue
        return text
    return ""


# ---------------------------------------------------------------------------
# 验证码暂停 + 等待用户手动处理
# ---------------------------------------------------------------------------
async def wait_for_captcha_solved(page: Page, cdp_port: int, max_wait_minutes: int = 15) -> bool:
    """检测到验证码后暂停，让用户用 chrome://inspect 远程过验证。

    返回 True 表示验证码已解决，False 表示超时。
    """
    print("\n" + "=" * 70, file=sys.stderr)
    print("⚠️  检测到豆包验证码 - 脚本已暂停，请手动处理", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(f"""
在你本地电脑做以下操作（如果之前的 SSH 隧道还在，跳过 1）：

  1) 新开一个本地终端：
       ssh -L {cdp_port}:127.0.0.1:{cdp_port}  你的用户名@虚拟机IP

  2) 本地 Chrome / Edge 地址栏访问：
       chrome://inspect/#devices

  3) 如果还没添加过，点 "Configure..." 添加 localhost:{cdp_port}

  4) "Remote Target" 区会出现豆包页面，点其下方的 "inspect"

  5) 在弹出的 DevTools 窗口里把验证码过掉（拖滑块/选图等）

脚本每 2 秒检查一次，验证码消失后自动继续（最长等 {max_wait_minutes} 分钟）。
按 Ctrl+C 可放弃等待。
""", file=sys.stderr, flush=True)

    start = time.time()
    deadline = start + max_wait_minutes * 60
    last_log_time = 0.0

    while time.time() < deadline:
        try:
            if not await captcha_visible(page):
                # 再确认一次（避免动画过渡期误判）
                await asyncio.sleep(1.0)
                if not await captcha_visible(page):
                    log("✅ 验证码已通过，继续执行")
                    await asyncio.sleep(1.5)  # 给页面一点时间稳定
                    return True
        except Exception as e:
            log(f"检测验证码状态异常：{e}")

        now = time.time()
        if now - last_log_time > 30:
            elapsed = int(now - start)
            log(f"... 仍在等待验证码完成（已等 {elapsed}s）")
            last_log_time = now
        await asyncio.sleep(2.0)

    log(f"⚠️ 等待验证码超过 {max_wait_minutes} 分钟，放弃")
    return False


# ---------------------------------------------------------------------------
# Cookie 加载（备用）
# ---------------------------------------------------------------------------
def _normalize_one(c: dict) -> dict:
    cookie = {
        "name": c["name"], "value": c["value"],
        "domain": c.get("domain", ".doubao.com"), "path": c.get("path", "/"),
        "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", False)),
    }
    exp = c.get("expirationDate", c.get("expires", c.get("expirationTime")))
    if exp not in (None, -1, 0, "session"):
        try: cookie["expires"] = int(float(exp))
        except (TypeError, ValueError): pass
    ss = c.get("sameSite", "Lax")
    if ss is None or str(ss).lower() in ("unspecified", "no_restriction", ""):
        ss = "None" if cookie["secure"] else "Lax"
    ss = str(ss).capitalize()
    if ss not in ("Strict", "Lax", "None"):
        ss = "Lax"
    if ss == "None": cookie["secure"] = True
    cookie["sameSite"] = ss
    return cookie


def _parse_netscape(content: str):
    out = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            if s.startswith("#HttpOnly_"): s = s[len("#HttpOnly_"):]
            else: continue
        parts = s.split("\t")
        if len(parts) < 7: continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        c = {"name": name, "value": value, "domain": domain, "path": path,
             "secure": secure.upper() == "TRUE"}
        try:
            exp = int(expires)
            if exp > 0: c["expires"] = exp
        except ValueError: pass
        out.append(c)
    return out


def load_cookies(cookies_file: str):
    raw = Path(cookies_file).read_text(encoding="utf-8").strip()
    if raw.startswith("[") or raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies") or data.get("Cookies") or []
        cookies = [_normalize_one(c) for c in data]
    else:
        cookies = [_normalize_one(c) for c in _parse_netscape(raw)]
    if not cookies:
        raise ValueError("没读到任何 cookie")
    return cookies


# ---------------------------------------------------------------------------
# 浏览器上下文构造
# ---------------------------------------------------------------------------
async def make_context(playwright, *, profile_dir, cookies, headless, cdp_port=None):
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    if cdp_port:
        args.append(f"--remote-debugging-port={cdp_port}")
        args.append("--remote-debugging-address=0.0.0.0")

    ctx_kwargs = dict(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )

    if profile_dir:
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()),
            headless=headless, args=args, **ctx_kwargs,
        )
        await context.add_init_script(STEALTH_JS)
        return None, context

    browser = await playwright.chromium.launch(headless=headless, args=args)
    context = await browser.new_context(**ctx_kwargs)
    await context.add_init_script(STEALTH_JS)
    if cookies:
        await context.add_cookies(cookies)
    return browser, context


async def safe_close(context, browser=None) -> None:
    """关闭时吞异常，避免 Ctrl+C 中断时报 'Connection closed while reading from the driver'。"""
    try:
        await context.close()
    except Exception as e:
        log(f"关闭 context 时出错（profile 通常已写入磁盘，忽略）：{type(e).__name__}: {e}")
    if browser:
        try:
            await browser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# login 子命令
# ---------------------------------------------------------------------------
async def cmd_login(args) -> int:
    log(f"启动持久化浏览器，profile：{args.profile_dir}")
    log(f"远程调试端口：{args.cdp_port}（仅通过 SSH 隧道访问，不要暴露公网）")

    async with async_playwright() as p:
        _, context = await make_context(
            p, profile_dir=args.profile_dir, cookies=None,
            headless=args.headless, cdp_port=args.cdp_port,
        )

        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            log(f"打开 {args.url} 出错（不影响登录）：{e}")

        port = args.cdp_port
        profile_abs = Path(args.profile_dir).resolve()
        print(f"""
======================================================================
浏览器已启动，请在你本地电脑完成登录。任选一种方式：
======================================================================

【方式 A：Chrome 远程调试】（推荐，无需图形转发）
  1) 本地新开终端：
       ssh -L {port}:127.0.0.1:{port}  你的用户名@虚拟机IP
  2) 本地 Chrome / Edge 访问：
       chrome://inspect/#devices
  3) 点 "Configure..."，添加 localhost:{port}，确认
  4) 几秒后 "Remote Target" 出现豆包页面，点其下方的 "inspect"
  5) DevTools 窗口里完成登录、过验证码

【方式 B：SSH X11 转发】
  ssh -X 你的用户名@虚拟机IP
  本命令加 --no-headless 重启

【方式 C：VNC】
  虚拟机装 x11vnc / TigerVNC，本命令加 --no-headless

完成后回到本终端按 Ctrl+C，配置保存到：
  {profile_abs}
======================================================================
""", file=sys.stderr, flush=True)

        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        def _stop(*_):
            if not stop.done():
                stop.set_result(None)

        try:
            loop.add_signal_handler(signal.SIGINT, _stop)
            loop.add_signal_handler(signal.SIGTERM, _stop)
        except NotImplementedError:
            pass

        async def heartbeat():
            warned = False
            while not stop.done():
                try:
                    if await captcha_visible(page) and not warned:
                        log("检测到验证码 - 请在远程浏览器里把它过掉")
                        warned = True
                    elif not await captcha_visible(page):
                        warned = False
                except Exception:
                    pass
                await asyncio.sleep(10)

        hb = asyncio.create_task(heartbeat())
        try:
            await stop
        finally:
            hb.cancel()

        log("正在保存 profile 并关闭浏览器...")
        await safe_close(context)
    return 0


# ---------------------------------------------------------------------------
# chat 子命令
# ---------------------------------------------------------------------------
async def dump_debug(page: Page, debug_path: Path, prefix: str) -> None:
    try:
        await page.screenshot(path=str(debug_path / f"{prefix}.png"), full_page=True)
    except Exception as e:
        log(f"截图失败：{e}")
    try:
        html = await page.evaluate("() => document.documentElement.outerHTML")
        (debug_path / f"{prefix}_dom.html").write_text(html, encoding="utf-8")
    except Exception as e:
        log(f"导出 DOM 失败：{e}")
    try:
        msgs = await get_messages(page)
        (debug_path / f"{prefix}_messages.json").write_text(
            json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log(f"导出消息列表失败：{e}")


async def handle_captcha_if_present(page: Page, cdp_port: int, debug_path: Path | None) -> None:
    """看到验证码就暂停等用户手动通过。失败抛 RuntimeError。"""
    if not await captcha_visible(page):
        return
    if debug_path:
        await dump_debug(page, debug_path, f"captcha_{int(time.time())}")
    ok = await wait_for_captcha_solved(page, cdp_port)
    if not ok:
        raise RuntimeError("等待验证码超时（默认 15 分钟）")


async def cmd_chat(args) -> int:
    input_text = Path(args.input).read_text(encoding="utf-8")
    if not input_text.strip():
        log("输入文件是空的")
        return 2

    cookies = None
    if args.cookies:
        cookies = load_cookies(args.cookies)
        log(f"加载 {len(cookies)} 条 cookie")

    if not args.profile_dir and not cookies:
        log("必须指定 --profile-dir（推荐）或 --cookies 之一")
        return 2

    debug_path = Path(args.debug_dir) if args.debug_dir else None
    if debug_path:
        debug_path.mkdir(parents=True, exist_ok=True)

    user_input_clean = input_text.strip()

    log(f"远程调试端口：{args.cdp_port}（出现验证码时可通过它远程过验证）")

    async with async_playwright() as p:
        browser, context = await make_context(
            p, profile_dir=args.profile_dir, cookies=cookies,
            headless=not args.no_headless,
            cdp_port=args.cdp_port,
        )
        try:
            page = (context.pages[0] if (args.profile_dir and context.pages)
                    else await context.new_page())

            log(f"打开 {args.url}")
            await page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass

            await asyncio.sleep(2.0)

            # 第 1 道：进入页面就有验证码
            await handle_captcha_if_present(page, args.cdp_port, debug_path)

            # 找输入框
            input_loc, input_sel = await find_first_visible(page, INPUT_SELECTORS, timeout_ms=15_000)
            if input_loc is None:
                if "login" in page.url.lower() or "passport" in page.url.lower():
                    raise RuntimeError(f"似乎跳到了登录页（{page.url}），需要重新 login")
                if debug_path:
                    await dump_debug(page, debug_path, "no_input")
                raise RuntimeError("找不到输入框，可能登录失效或页面结构变了")
            log(f"找到输入框：{input_sel}")

            before = await get_messages(page)
            log(f"消息提取策略：{before.get('strategy')}，发送前 {before.get('count', 0)} 条")
            before_set = set(before.get("messages", []))
            before_ids = {
                str(item.get("id"))
                for item in (before.get("items") or [])
                if isinstance(item, dict) and item.get("id")
            }

            # 输入
            await input_loc.click()
            await asyncio.sleep(0.3)
            tag = (await input_loc.evaluate("el => el.tagName")).lower()
            if tag == "textarea":
                await input_loc.fill(input_text)
            else:
                try:
                    await input_loc.fill(input_text)
                except Exception:
                    await page.keyboard.type(input_text, delay=5)
            await asyncio.sleep(0.5)

            # 发送
            sent = False
            for sel in SEND_BUTTON_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500) and await btn.is_enabled():
                        await btn.click()
                        sent = True
                        log(f"点击发送按钮：{sel}")
                        break
                except Exception:
                    continue
            if not sent:
                log("未找到发送按钮，按 Enter")
                await input_loc.press("Enter")

            # 第 2 道：发送后短时间内出现验证码
            await asyncio.sleep(1.5)
            await handle_captcha_if_present(page, args.cdp_port, debug_path)

            # 等豆包回复出现
            log("等待豆包回复出现...")
            reply_text = ""
            appear_start = time.time()
            while time.time() - appear_start < 60:
                # 第 3 道：等待过程中出现验证码
                await handle_captcha_if_present(page, args.cdp_port, debug_path)

                cur = await get_messages(page)
                reply_text = latest_assistant_message(
                    cur,
                    user_input_clean,
                    before_ids=before_ids,
                    before_texts=before_set,
                )
                if reply_text:
                    log(f"检测到回复（{len(reply_text)} 字符），开始监听变化")
                    break
                await asyncio.sleep(0.5)

            if not reply_text:
                log("60 秒内未确认回复，进入容错轮询模式")

            # 轮询稳定
            log(f"轮询：{args.stable_seconds}s 不变即视为完成（最长 {args.max_wait}s）")
            last_text = reply_text
            last_change = time.time()
            poll_start = time.time()

            while True:
                if time.time() - poll_start > args.max_wait:
                    log("达到最长等待时间")
                    break

                # 第 4 道：流式回复中也可能弹验证码
                await handle_captcha_if_present(page, args.cdp_port, debug_path)

                cur = await get_messages(page)
                cur_reply = latest_assistant_message(
                    cur,
                    user_input_clean,
                    before_ids=before_ids,
                    before_texts=before_set,
                )

                if cur_reply and cur_reply != last_text:
                    last_text = cur_reply
                    last_change = time.time()
                elif cur_reply and (time.time() - last_change) >= args.stable_seconds:
                    if not await is_anything_visible(page, STOP_BUTTON_SELECTORS):
                        log(f"稳定 {args.stable_seconds:.1f}s，认为完成")
                        break

                await asyncio.sleep(0.5)

            if not last_text:
                if debug_path:
                    await dump_debug(page, debug_path, "empty_reply")
                raise RuntimeError(
                    "读到的回复是空的。" + (
                        "已 dump DOM 到 debug 目录，把 *_dom.html 和 *_messages.json 发我看下。"
                        if debug_path else
                        "加 --debug-dir ./debug 重跑可保存 DOM 用于排查。"
                    )
                )

            Path(args.output).write_text(last_text, encoding="utf-8")
            log(f"回复已写入 {args.output}（{len(last_text)} 字符）")

            if debug_path:
                await dump_debug(page, debug_path, "success")
            return 0

        finally:
            await safe_close(context, browser)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="豆包网页版自动化（持久化 profile + JS 启发式提取 + 验证码暂停）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="一次性交互式登录，建立持久化浏览器配置")
    p_login.add_argument("--profile-dir", required=True, help="浏览器 profile 目录")
    p_login.add_argument("--cdp-port", type=int, default=9222, help="远程调试端口")
    p_login.add_argument("--url", default="https://www.doubao.com/", help="登录入口 URL")
    p_login.add_argument("--no-headless", dest="headless", action="store_false",
                         help="非无头模式（需要 X 转发或 VNC）")
    p_login.set_defaults(headless=True)

    p_chat = sub.add_parser("chat", help="发送消息并保存回复")
    p_chat.add_argument("-i", "--input", required=True, help="输入文件")
    p_chat.add_argument("-o", "--output", required=True, help="输出文件")
    p_chat.add_argument("--profile-dir", help="持久化浏览器配置目录（推荐）")
    p_chat.add_argument("-c", "--cookies", help="cookie 文件（备用）")
    p_chat.add_argument("-u", "--url",
                        default="https://www.doubao.com/chat/38423998172211970",
                        help="豆包对话页面 URL")
    p_chat.add_argument("--no-headless", action="store_true", help="非无头模式")
    p_chat.add_argument("--cdp-port", type=int, default=9222,
                        help="远程调试端口（验证码弹出时通过它手动处理）")
    p_chat.add_argument("--stable-seconds", type=float, default=4.0,
                        help="回复多少秒不变就视为完成")
    p_chat.add_argument("--max-wait", type=float, default=60.0, help="最长等待秒数")
    p_chat.add_argument("--debug-dir", default=None, help="保存截图、DOM、消息列表")

    args = ap.parse_args()

    try:
        if args.cmd == "login":
            return asyncio.run(cmd_login(args))
        elif args.cmd == "chat":
            return asyncio.run(cmd_chat(args))
    except KeyboardInterrupt:
        log("用户中断")
        return 130
    except Exception as e:
        print(f"[doubao] 出错：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
