#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weibo_tool.py
==============

微博桌面网页版自动化工具 for nova。

设计目标：
- 不修改 nova 本体代码；nova 只需通过 shell 调用本脚本。
- 只使用微博桌面网页版 https://weibo.com，不默认访问 m.weibo.cn。
- 所有命令 stdout 只输出一个 JSON 对象，方便 nova 解析。
- 登录、验证码、账号安全检查由人类手动处理；工具不绕过验证码/风控。
- 写操作通过桌面网页版 UI 完成，并在成功后尽量验证。
- 所有失败返回稳定 error_code，并保存 debug artifacts。

注意：微博桌面网页版 DOM 会变。本工具是工程化第一版：命令协议稳定，选择器/路径可按 debug 文件继续修。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import signal
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse, parse_qs

try:
    from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout
except Exception:  # pragma: no cover
    async_playwright = None
    BrowserContext = Any  # type: ignore
    Page = Any  # type: ignore
    PWTimeout = TimeoutError  # type: ignore

DEFAULT_PROFILE = os.path.expanduser("~/.nova_profiles/weibo-default")
DEFAULT_STATE_DIR = os.path.expanduser("~/nova_workspace/state/weibo")
DEFAULT_POLICY = os.path.expanduser("~/nova_workspace/config/weibo_policy.json")
DESKTOP_HOME = "https://weibo.com/"
DESKTOP_SEARCH = "https://s.weibo.com/weibo"
USER_AGENT_DESKTOP = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

ERROR_NEEDS_LOGIN = "NEEDS_LOGIN"
ERROR_CAPTCHA = "CAPTCHA_REQUIRED"
ERROR_SECURITY = "ACCOUNT_SECURITY_CHECK"
ERROR_TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
ERROR_AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
ERROR_COMPOSER_NOT_FOUND = "COMPOSER_NOT_FOUND"
ERROR_PUBLISH_FAILED = "PUBLISH_FAILED"
ERROR_VERIFY_FAILED = "VERIFY_FAILED"
ERROR_RATE_LIMITED = "RATE_LIMITED"
ERROR_POLICY_DENIED = "POLICY_DENIED"
ERROR_NETWORK = "NETWORK_ERROR"
ERROR_SELECTOR = "SELECTOR_BROKEN"
ERROR_DEP = "TOOL_MISSING_DEPENDENCY"
ERROR_UNKNOWN = "UNKNOWN_ERROR"

WRITE_ACTIONS = {"post", "comment", "reply-comment", "repost", "like", "unlike", "delete-post", "delete-comment"}


class ToolError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status: str = "failed",
        blocked_reason: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status = status
        self.blocked_reason = blocked_reason
        self.data = data or {}


@dataclasses.dataclass
class RuntimeOptions:
    profile: str = DEFAULT_PROFILE
    state_dir: str = DEFAULT_STATE_DIR
    policy: str = DEFAULT_POLICY
    headless: bool = True
    cdp_port: Optional[int] = None
    timeout_ms: int = 45_000
    debug: bool = False
    json_stdout: bool = True


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, flush=True, **kwargs)


def ensure_dir(path: str | Path) -> Path:
    p = Path(path).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_debug_dir(opts: RuntimeOptions, action: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = ensure_dir(Path(opts.state_dir).expanduser() / "debug" / f"{stamp}_{action}")
    return p


def strip_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\r", "\n")
    s = re.sub(r"[\t\f\v]+", " ", s)
    s = re.sub(r"[ \u00a0]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_text_file(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8")


def load_policy(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ToolError(ERROR_POLICY_DENIED, f"policy 文件无法解析：{p}: {exc}", data={"policy_path": str(p)})


def policy_allowed(policy: dict, action: str) -> bool:
    autonomy = policy.get("autonomy") or {}
    key_map = {
        "post": "can_post",
        "comment": "can_comment",
        "reply-comment": "can_reply_comment",
        "repost": "can_repost",
        "like": "can_like",
        "unlike": "can_like",
        "delete-post": "can_delete_own_recent_posts",
        "delete-comment": "can_delete_own_recent_posts",
    }
    key = key_map.get(action)
    if key is None:
        return True
    return autonomy.get(key, True) is not False


def action_log_path(opts: RuntimeOptions) -> Path:
    return ensure_dir(opts.state_dir) / "actions.jsonl"


def append_action_log(opts: RuntimeOptions, record: dict) -> None:
    p = action_log_path(opts)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_duplicate(opts: RuntimeOptions, dedupe_key: str, window_seconds: int = 600) -> Optional[dict]:
    p = action_log_path(opts)
    if not p.exists():
        return None
    now = time.time()
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-500:]
        for line in reversed(lines):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("dedupe_key") == dedupe_key and obj.get("ok"):
                ts = obj.get("epoch") or 0
                if now - float(ts) <= window_seconds:
                    return obj
    except Exception:
        return None
    return None


def parse_weibo_id(value: str) -> str:
    """Extract numeric mid/id or alphanumeric bid from a desktop Weibo URL or raw id."""
    v = (value or "").strip()
    if not v:
        raise ToolError(ERROR_TARGET_NOT_FOUND, "缺少微博链接或 ID")
    parsed = urlparse(v)
    if parsed.scheme:
        qs = parse_qs(parsed.query)
        for k in ("id", "mid", "mblogid"):
            if qs.get(k):
                return qs[k][0]
        parts = [p for p in parsed.path.split("/") if p]
        # Desktop examples:
        # https://weibo.com/1234567890/NabcDEF12
        # https://weibo.com/detail/5012345678901234
        # https://weibo.com/u/1234567890
        for part in reversed(parts):
            if re.fullmatch(r"[0-9A-Za-z]+", part) and part not in {"u", "detail", "status", "profile"}:
                return part
    m = re.search(r"(?:weibo\.com/(?:detail/|\d+/)|/status/)([0-9A-Za-z]+)", v)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9A-Za-z]+", v):
        return v
    raise ToolError(ERROR_TARGET_NOT_FOUND, f"无法从输入中识别微博 ID：{value}")


def parse_uid(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ToolError(ERROR_TARGET_NOT_FOUND, "缺少用户链接或 UID")
    parsed = urlparse(v)
    if parsed.scheme:
        qs = parse_qs(parsed.query)
        for k in ("uid", "value"):
            if qs.get(k):
                return qs[k][0]
        parts = [p for p in parsed.path.split("/") if p]
        for i, part in enumerate(parts):
            if part in {"u", "profile"} and i + 1 < len(parts) and parts[i + 1].isdigit():
                return parts[i + 1]
            if part.isdigit() and len(part) >= 5:
                return part
    if re.fullmatch(r"\d{5,}", v):
        return v
    raise ToolError(ERROR_TARGET_NOT_FOUND, f"无法从输入中识别 UID：{value}")


def normalize_count(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = strip_text(value).replace(",", "").strip()
    if not s:
        return None
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100000000)
        m = re.search(r"\d+(?:\.\d+)?", s)
        if m:
            return int(float(m.group(0)))
    except Exception:
        return None
    return None


def parse_metrics_from_text(text: str) -> dict:
    t = strip_text(text)
    metrics = {"likes": None, "comments": None, "reposts": None, "views": None}
    patterns = [
        ("reposts", [r"转发\s*([0-9.万亿]+)", r"([0-9.万亿]+)\s*转发"]),
        ("comments", [r"评论\s*([0-9.万亿]+)", r"([0-9.万亿]+)\s*评论"]),
        ("likes", [r"赞\s*([0-9.万亿]+)", r"([0-9.万亿]+)\s*赞", r"点赞\s*([0-9.万亿]+)"]),
        ("views", [r"阅读\s*([0-9.万亿]+)", r"浏览\s*([0-9.万亿]+)", r"([0-9.万亿]+)\s*阅读"]),
    ]
    for key, pats in patterns:
        for pat in pats:
            m = re.search(pat, t)
            if m:
                metrics[key] = normalize_count(m.group(1))
                break
    return metrics


def desktop_post_url_from_id(post_id: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    if fallback and fallback.startswith("http"):
        return fallback
    if post_id:
        return f"https://weibo.com/detail/{post_id}"
    return None


def compact_visible_text(text: str, max_len: int = 1200) -> str:
    t = strip_text(text)
    # Remove common desktop action clutter without being too aggressive.
    lines = [ln.strip() for ln in t.splitlines()]
    cleaned: List[str] = []
    skip_exact = {"转发", "评论", "赞", "收藏", "分享", "举报", "关注", "已关注", "发布", "发送"}
    for ln in lines:
        if not ln or ln in skip_exact:
            continue
        if re.fullmatch(r"(转发|评论|赞)\s*\d*", ln):
            continue
        cleaned.append(ln)
    out = "\n".join(cleaned)
    if len(out) > max_len:
        out = out[:max_len].rstrip() + "..."
    return out


class WeiboSession:
    def __init__(self, opts: RuntimeOptions) -> None:
        self.opts = opts
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.debug_dir: Optional[Path] = None
        self.debug_artifacts: List[str] = []

    async def __aenter__(self) -> "WeiboSession":
        if async_playwright is None:
            raise ToolError(ERROR_DEP, "缺少 playwright。请运行：pip install -r requirements.txt && playwright install chromium", status="blocked", blocked_reason="missing_dependency")
        self.playwright = await async_playwright().start()
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--lang=zh-CN",
            # 不使用 stealth/anti-detect 参数；保持普通持久化浏览器会话。
        ]
        if self.opts.cdp_port:
            args += [
                f"--remote-debugging-port={self.opts.cdp_port}",
                "--remote-debugging-address=0.0.0.0",
            ]
        profile = Path(self.opts.profile).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile.resolve()),
            headless=self.opts.headless,
            args=args,
            user_agent=USER_AGENT_DESKTOP,
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(self.opts.timeout_ms)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self.context:
                await self.context.close()
        except Exception as e:
            eprint(f"关闭浏览器上下文失败，忽略：{type(e).__name__}: {e}")
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

    async def ensure_page(self) -> Page:
        if not self.page:
            raise RuntimeError("page not initialized")
        return self.page

    async def debug_dump(self, action: str, extra: Optional[dict] = None) -> None:
        if not self.opts.debug and extra is None:
            return
        if self.debug_dir is None:
            self.debug_dir = make_debug_dir(self.opts, action)
        page = self.page
        if page:
            try:
                png = self.debug_dir / f"{action}.png"
                await page.screenshot(path=str(png), full_page=True)
                self.debug_artifacts.append(str(png))
            except Exception:
                pass
            try:
                html_path = self.debug_dir / f"{action}.html"
                html_path.write_text(await page.content(), encoding="utf-8")
                self.debug_artifacts.append(str(html_path))
            except Exception:
                pass
        if extra is not None:
            if self.debug_dir is None:
                self.debug_dir = make_debug_dir(self.opts, action)
            p = self.debug_dir / f"{action}.json"
            p.write_text(json.dumps(extra, ensure_ascii=False, indent=2), encoding="utf-8")
            self.debug_artifacts.append(str(p))

    async def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        page = await self.ensure_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=self.opts.timeout_ms)
        except PWTimeout:
            # 微博 React 页面经常保持网络请求，DOM 到了即可继续。
            pass
        await asyncio.sleep(1.2)
        await self.detect_blockers()

    async def cookies_summary(self) -> dict:
        if not self.context:
            return {}
        cookies = await self.context.cookies(["https://weibo.com", "https://passport.weibo.com", "https://s.weibo.com"])
        names = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
        likely_logged = any(names.get(n) for n in ["SUB", "SUBP", "SSOLoginState", "ALF"])
        return {
            "likely_logged_in": bool(likely_logged),
            "cookie_names": sorted([n for n in names.keys() if n in {"SUB", "SUBP", "SSOLoginState", "ALF", "XSRF-TOKEN", "WBPSESS"}]),
        }

    async def detect_blockers(self) -> None:
        """检测真正的阻塞页面（验证码 / 账号安全验证）。

        历史教训：微博桌面网页版登录后的菜单/页脚里就含有"账号安全""安全中心"
        等链接，"风险"作为普通中文词在热搜/广告/新闻里到处出现。如果用宽松的
        关键词扫整个 body，会把已登录的正常首页也判成 ACCOUNT_SECURITY_CHECK。

        因此这里采用三层判断，且只用具体到不可能出现在普通菜单里的短语：
          1) URL：被重定向到 security.weibo.com 或 passport 验证路径 → 阻塞
          2) Body：必须出现明确的"请验证 / 滑块 / 安全验证 / 账号被限制"等
             完整短语，单独的"账号安全""风险""安全中心"不再触发。
        """
        page = await self.ensure_page()
        try:
            url = page.url or ""
            url_l = url.lower()
            try:
                parsed = urlparse(url_l)
                host = parsed.netloc or ""
                path = parsed.path or ""
            except Exception:
                host = ""
                path = ""

            # 1) URL 级别：明确跳转到安全/验证子站
            if host.startswith("security.weibo.com") or path.startswith("/security/check"):
                await self.debug_dump("account_security_detected_url")
                raise ToolError(
                    ERROR_SECURITY,
                    "微博跳转到安全/验证页面，需要人类手动处理。",
                    status="blocked",
                    blocked_reason="account_security_check",
                    data={"url": url},
                )
            # passport.weibo.com 上的可视化校验/风控验证路径
            if host == "passport.weibo.com" and any(
                seg in path for seg in ("/visible/", "/aj/sso/checkverifycode", "/aj/sso/verify", "/sso/login")
            ):
                # 这种情况通常是 SSO 验证或风控 visible，需要人工
                # 但单纯的 passport.weibo.com/login 不算，所以要更具体
                if "/visible/" in path or "/verify" in path or "/checkverify" in path:
                    await self.debug_dump("account_security_detected_passport")
                    raise ToolError(
                        ERROR_SECURITY,
                        "微博跳转到 passport 验证页面，需要人类手动处理。",
                        status="blocked",
                        blocked_reason="account_security_check",
                        data={"url": url},
                    )

            title = ""
            try:
                title = (await page.title()) or ""
            except Exception:
                pass
            body = ""
            try:
                body = await page.locator("body").inner_text(timeout=2500)
            except Exception:
                pass
            # 限制长度
            body_for_match = body[:12000]
            joined = title + "\n" + body_for_match

            # 2) 验证码 / 滑块（需要完整且具体的短语，避免菜单里"安全验证"误触发）
            captcha_phrases = [
                "请完成下方验证",
                "请完成安全验证",
                "请先完成安全验证",
                "请通过安全验证",
                "拖动滑块完成拼图",
                "拖动下方滑块",
                "按住滑块",
                "向右滑动完成验证",
                "请输入图片验证码",
                "请输入验证码",
                "图形验证码",
                "请完成图形验证",
            ]
            if any(p in joined for p in captcha_phrases):
                await self.debug_dump("captcha_detected")
                raise ToolError(
                    ERROR_CAPTCHA,
                    "微博出现验证码或安全验证，需要人类手动处理。",
                    status="blocked",
                    blocked_reason="captcha_required",
                )
            # 英文 captcha 标志（保留少量）
            joined_l = joined.lower()
            if "please complete the captcha" in joined_l or "security verification required" in joined_l:
                await self.debug_dump("captcha_detected_en")
                raise ToolError(
                    ERROR_CAPTCHA,
                    "微博出现验证码或安全验证，需要人类手动处理。",
                    status="blocked",
                    blocked_reason="captcha_required",
                )

            # 3) 账号安全 / 风控（必须是完整的提示语，而不是菜单里"账号安全""风险"链接）
            security_phrases = [
                "您的账号存在异常",
                "你的账号存在异常",
                "您的账号存在安全风险",
                "你的账号存在安全风险",
                "账号异常，请",
                "为了您的账号安全，请",
                "为了你的账号安全，请",
                "请验证身份后继续",
                "请验证身份后访问",
                "需要进行安全验证",
                "请通过验证后继续",
                "您的账号已被限制",
                "你的账号已被限制",
                "您的账号已被冻结",
                "你的账号已被冻结",
                "账号已被冻结",
                "账号已被锁定",
                "解除账号限制",
                "请先解除账号限制",
                "账号存在被盗风险",
                "检测到您的账号存在异常",
                "检测到你的账号存在异常",
            ]
            if any(p in joined for p in security_phrases):
                await self.debug_dump("account_security_detected")
                raise ToolError(
                    ERROR_SECURITY,
                    "微博出现账号安全或风险提示，需要人类手动处理。",
                    status="blocked",
                    blocked_reason="account_security_check",
                )
        except ToolError:
            raise
        except Exception:
            return

    async def is_logged_in(self) -> bool:
        summary = await self.cookies_summary()
        if summary.get("likely_logged_in"):
            return True
        page = await self.ensure_page()
        try:
            body = await page.locator("body").inner_text(timeout=2000)
            looks_like_login_page = (
                re.search(r"(扫码登录|短信登录|账号登录|登录微博|手机号登录)", body)
                and not re.search(r"(我的首页|我的关注|热搜|消息|发微博)", body)
            )
            if looks_like_login_page:
                return False
            # body 里出现明显的登录后元素就认为已登录（cookie 名字可能因微博改版换过）
            if re.search(r"(我的首页|发微博|关注我的|消息盒子|我的关注)", body):
                return True
        except Exception:
            pass
        return False

    async def require_login(self) -> None:
        await self.goto(DESKTOP_HOME)
        if not await self.is_logged_in():
            await self.debug_dump("needs_login")
            raise ToolError(ERROR_NEEDS_LOGIN, "当前 profile 未登录微博桌面网页版，或登录态已失效。请运行 login。", status="blocked", blocked_reason="needs_login")

    async def whoami(self) -> dict:
        await self.goto(DESKTOP_HOME)
        if not await self.is_logged_in():
            raise ToolError(ERROR_NEEDS_LOGIN, "当前 profile 未登录微博桌面网页版。", status="blocked", blocked_reason="needs_login")
        page = await self.ensure_page()
        js = r"""
        () => {
          const cfg = window.$CONFIG || window.__CONFIG__ || {};
          const out = {
            uid: cfg.uid || cfg.$uid || cfg.user_id || null,
            nickname: cfg.nick || cfg.nickname || cfg.screen_name || null,
            profile_url: null,
            candidates: []
          };
          const links = Array.from(document.querySelectorAll('a[href]'));
          for (const a of links) {
            const href = a.href || '';
            const txt = (a.innerText || a.getAttribute('title') || '').trim();
            if (/weibo\.com\/(u\/)?\d{5,}/.test(href)) {
              out.candidates.push({href, text: txt});
            }
          }
          const preferred = out.candidates.find(x => /\/u\/\d{5,}/.test(x.href)) || out.candidates[0];
          if (preferred) {
            out.profile_url = preferred.href;
            if (!out.nickname && preferred.text && preferred.text.length < 40) out.nickname = preferred.text;
            const m = preferred.href.match(/(?:\/u\/|weibo\.com\/)(\d{5,})/);
            if (m && !out.uid) out.uid = m[1];
          }
          try {
            for (let i = 0; i < localStorage.length; i++) {
              const k = localStorage.key(i) || '';
              const v = localStorage.getItem(k) || '';
              if (!out.uid) {
                const m = v.match(/"uid"\s*:\s*"?(\d{5,})"?/);
                if (m) out.uid = m[1];
              }
              if (!out.nickname) {
                const m = v.match(/"screen_name"\s*:\s*"([^"\\]{1,40})"/);
                if (m) out.nickname = m[1];
              }
            }
          } catch (e) {}
          return out;
        }
        """
        info = await page.evaluate(js)
        uid = str(info.get("uid") or "") or None
        profile_url = info.get("profile_url") or (f"https://weibo.com/u/{uid}" if uid else None)
        return {
            "nickname": info.get("nickname"),
            "uid": uid,
            "profile_url": profile_url,
            "login_state": "logged_in",
            "site": "desktop_weibo_com",
            "raw_subset": {"candidate_count": len(info.get("candidates") or [])},
        }

    async def scroll_page(self, rounds: int = 3) -> None:
        page = await self.ensure_page()
        for _ in range(max(0, rounds)):
            await page.mouse.wheel(0, 1200)
            await asyncio.sleep(0.9)
            await self.detect_blockers()

    async def extract_cards(self, limit: int = 20) -> List[dict]:
        page = await self.ensure_page()
        js = r"""
        ({limit}) => {
          function visible(el) {
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 50 && r.height > 30 && st.visibility !== 'hidden' && st.display !== 'none';
          }
          function postHrefFrom(el) {
            const links = Array.from(el.querySelectorAll('a[href]'));
            const hit = links.find(a => {
              const href = a.href || '';
              return /weibo\.com\/(detail\/\w+|\d{5,}\/\w+)/.test(href) || /\/status\//.test(href);
            });
            return hit ? hit.href : null;
          }
          function authorFrom(el) {
            const links = Array.from(el.querySelectorAll('a[href]'));
            const hit = links.find(a => {
              const href = a.href || '';
              return /weibo\.com\/(u\/)?\d{5,}/.test(href) && !/\/detail\//.test(href);
            });
            return hit ? {href: hit.href, text: (hit.innerText || hit.getAttribute('title') || '').trim()} : {href:null,text:null};
          }
          const selectors = [
            'article',
            '[mid]', '[data-mid]', '[data-id]',
            '[action-type="feed_list_item"]',
            '[modal-type="feed"]',
            'div[class*="Feed"]',
            'div[class*="feed"]',
            'div[class*="card"]'
          ];
          let nodes = [];
          for (const sel of selectors) {
            try { nodes.push(...Array.from(document.querySelectorAll(sel))); } catch(e) {}
          }
          // Fallback: climb from post links to a reasonably large parent.
          for (const a of Array.from(document.querySelectorAll('a[href]'))) {
            const href = a.href || '';
            if (!/weibo\.com\/(detail\/\w+|\d{5,}\/\w+)/.test(href)) continue;
            let n = a;
            for (let i = 0; i < 5 && n && n.parentElement; i++) {
              n = n.parentElement;
              const text = (n.innerText || '').trim();
              if (text.length > 40) { nodes.push(n); break; }
            }
          }
          const out = [];
          const seen = new Set();
          for (const n of nodes) {
            if (!visible(n)) continue;
            let text = (n.innerText || '').trim();
            if (!text || text.length < 8) continue;
            if (text.length > 3000) text = text.slice(0, 3000);
            const href = postHrefFrom(n);
            const author = authorFrom(n);
            const mid = n.getAttribute('mid') || n.getAttribute('data-mid') || n.getAttribute('data-id') || null;
            const key = mid || href || text.slice(0, 120);
            if (seen.has(key)) continue;
            seen.add(key);
            const timeEl = Array.from(n.querySelectorAll('a, span, time')).find(x => /刚刚|分钟前|小时前|今天|昨天|\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2}/.test((x.innerText || '').trim()));
            out.push({
              post_id: mid,
              post_url: href,
              author_name: author.text,
              author_url: author.href,
              text,
              created_at: timeEl ? (timeEl.innerText || '').trim() : null
            });
            if (out.length >= limit) break;
          }
          return out;
        }
        """
        raw = await page.evaluate(js, {"limit": limit * 3})
        items: List[dict] = []
        seen = set()
        for r in raw or []:
            href = r.get("post_url")
            pid = r.get("post_id")
            if not pid and href:
                try:
                    pid = parse_weibo_id(href)
                except Exception:
                    pid = None
            text = compact_visible_text(r.get("text") or "")
            key = pid or href or text[:120]
            if not text or key in seen:
                continue
            seen.add(key)
            items.append({
                "post_id": pid,
                "author_name": r.get("author_name"),
                "author_url": r.get("author_url"),
                "text": text,
                "post_url": desktop_post_url_from_id(pid, href),
                "created_at": r.get("created_at"),
                "metrics": parse_metrics_from_text(text),
                "source": "desktop_dom",
            })
            if len(items) >= limit:
                break
        return items

    async def feed(self, kind: str = "home", limit: int = 20) -> dict:
        await self.require_login()
        url_map = {
            "home": DESKTOP_HOME,
            "following": "https://weibo.com/mygroups?gid=110001000000000",
            "hot": "https://weibo.com/hot/search",
        }
        url = url_map.get(kind, DESKTOP_HOME)
        await self.goto(url)
        await self.scroll_page(rounds=max(2, min(8, limit // 5 + 1)))
        items = await self.extract_cards(limit=limit)
        return {"kind": kind, "items": items, "warnings": [] if items else ["未从桌面 DOM 提取到微博卡片，可能页面结构变化或未加载。"]}

    async def search(self, query: str, sort: str = "time", limit: int = 20) -> dict:
        await self.require_login()
        q = quote(query)
        # s.weibo.com 是微博桌面搜索，不是移动端。xsort=time 尽量按时间，但微博可能忽略。
        extra = "&xsort=time" if sort == "time" else "&Refer=weibo_weibo"
        await self.goto(f"{DESKTOP_SEARCH}?q={q}{extra}")
        await self.scroll_page(rounds=max(2, min(8, limit // 5 + 1)))
        items = await self.extract_cards(limit=limit)
        return {"query": query, "sort": sort, "items": items, "warnings": [] if items else ["未提取到搜索结果，可能被登录/搜索限制或页面结构变化。"]}

    async def user(self, url_or_uid: str, limit: int = 20) -> dict:
        await self.require_login()
        if url_or_uid.startswith("http"):
            url = url_or_uid
            uid = None
            try:
                uid = parse_uid(url_or_uid)
            except Exception:
                pass
        else:
            uid = parse_uid(url_or_uid)
            url = f"https://weibo.com/u/{uid}"
        await self.goto(url)
        await self.scroll_page(rounds=max(2, min(8, limit // 5 + 1)))
        items = await self.extract_cards(limit=limit)
        profile = await self.extract_profile_hint()
        return {"user": {"uid": uid, "url": url, **profile}, "items": items, "warnings": [] if items else ["未提取到用户主页微博，可能无权限、未加载或页面结构变化。"]}

    async def extract_profile_hint(self) -> dict:
        page = await self.ensure_page()
        js = r"""
        () => {
          const body = (document.body.innerText || '').split('\n').map(x => x.trim()).filter(Boolean);
          const title = document.title || '';
          const h = Array.from(document.querySelectorAll('h1,h2,h3,[class*="name"],[class*="Name"]')).map(x => (x.innerText || '').trim()).filter(Boolean).slice(0, 5);
          return {title, headings: h, first_lines: body.slice(0, 20)};
        }
        """
        raw = await page.evaluate(js)
        nickname = None
        for s in raw.get("headings") or []:
            if 1 <= len(s) <= 40 and not re.search(r"微博|首页|搜索", s):
                nickname = s
                break
        return {"nickname_hint": nickname, "page_title": raw.get("title")}

    async def post_detail(self, url_or_id: str, include_comments: bool = False, limit_comments: int = 30) -> dict:
        await self.require_login()
        url = url_or_id if url_or_id.startswith("http") else f"https://weibo.com/detail/{parse_weibo_id(url_or_id)}"
        await self.goto(url)
        await self.scroll_page(rounds=2 if include_comments else 0)
        items = await self.extract_cards(limit=5)
        post = items[0] if items else await self.extract_main_page_as_post(url)
        comments: List[dict] = []
        if include_comments:
            comments = await self.extract_comments(limit=limit_comments)
        return {"post": post, "comments": comments, "warnings": [] if post else ["未能可靠提取微博详情。"]}

    async def extract_main_page_as_post(self, url: str) -> dict:
        page = await self.ensure_page()
        try:
            body = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        pid = None
        try:
            pid = parse_weibo_id(url)
        except Exception:
            pass
        return {
            "post_id": pid,
            "author_name": None,
            "author_url": None,
            "text": compact_visible_text(body, max_len=1800),
            "post_url": url,
            "created_at": None,
            "metrics": parse_metrics_from_text(body),
            "source": "desktop_body_fallback",
        }

    async def extract_comments(self, limit: int = 30) -> List[dict]:
        page = await self.ensure_page()
        js = r"""
        ({limit}) => {
          const candidates = [];
          const sels = [
            '[class*="comment"]', '[class*="Comment"]',
            '[node-type*="comment"]', '[data-comment-id]', '[comment_id]'
          ];
          for (const sel of sels) {
            try { candidates.push(...Array.from(document.querySelectorAll(sel))); } catch(e) {}
          }
          const out = [];
          const seen = new Set();
          for (const n of candidates) {
            const text = (n.innerText || '').trim();
            if (!text || text.length < 3 || text.length > 1200) continue;
            const cid = n.getAttribute('data-comment-id') || n.getAttribute('comment_id') || n.getAttribute('mid') || null;
            const author = Array.from(n.querySelectorAll('a[href]')).find(a => /weibo\.com\/(u\/)?\d{5,}/.test(a.href || ''));
            const key = cid || text.slice(0, 80);
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({comment_id: cid, text, author_name: author ? (author.innerText || '').trim() : null, author_url: author ? author.href : null});
            if (out.length >= limit) break;
          }
          return out;
        }
        """
        raw = await page.evaluate(js, {"limit": limit})
        comments: List[dict] = []
        for c in raw or []:
            comments.append({
                "comment_id": c.get("comment_id"),
                "post_id": None,
                "author_name": c.get("author_name"),
                "author_url": c.get("author_url"),
                "text": compact_visible_text(c.get("text") or "", max_len=800),
                "created_at": None,
                "likes": normalize_count(c.get("text")),
                "comment_url": None,
                "source": "desktop_dom",
            })
        return comments[:limit]

    async def notifications(self, kind: str = "comments", limit: int = 30) -> dict:
        await self.require_login()
        url_map = {
            "comments": "https://weibo.com/message/comment",
            "mentions": "https://weibo.com/at/weibo",
            "likes": "https://weibo.com/message/like",
            "reposts": "https://weibo.com/message/repost",
            "all": "https://weibo.com/message",
        }
        await self.goto(url_map.get(kind, "https://weibo.com/message"))
        await self.scroll_page(rounds=max(2, min(8, limit // 5 + 1)))
        cards = await self.extract_cards(limit=limit)
        comments = await self.extract_comments(limit=limit)
        items: List[dict] = []
        for c in comments:
            c["notification_kind"] = kind
            items.append(c)
        for m in cards:
            m["notification_kind"] = kind
            items.append(m)
        return {"kind": kind, "items": items[:limit], "warnings": [] if items else ["未从通知页提取到通知，可能页面路径变化或无通知。"]}

    async def stats(self, mine: bool = False, post_url: Optional[str] = None, limit: int = 20) -> dict:
        await self.require_login()
        if post_url:
            detail = await self.post_detail(post_url, include_comments=False)
            post = detail.get("post") or {}
            return {"items": [post], "warnings": detail.get("warnings", [])}
        if mine:
            info = await self.whoami()
            url = info.get("profile_url") or DESKTOP_HOME
            await self.goto(url)
            await self.scroll_page(rounds=max(2, min(8, limit // 5 + 1)))
            items = await self.extract_cards(limit=limit)
            return {"items": items, "warnings": [] if items else ["未提取到自己的微博数据。"]}
        data = await self.feed(kind="home", limit=limit)
        return {"items": data.get("items", []), "warnings": data.get("warnings", [])}

    async def find_textbox(self, purpose: str = "generic"):
        page = await self.ensure_page()
        # Prefer visible contenteditable editors. Desktop Weibo often uses contenteditable divs.
        selectors_by_purpose = {
            "post": [
                '[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"]',
                'textarea',
            ],
            "comment": [
                'textarea',
                '[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"]',
            ],
            "generic": [
                '[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"]',
                'textarea',
            ],
        }
        selectors = selectors_by_purpose.get(purpose, selectors_by_purpose["generic"])
        for sel in selectors:
            loc = page.locator(sel)
            count = 0
            try:
                count = await loc.count()
            except Exception:
                continue
            for i in range(min(count, 12)):
                cand = loc.nth(i)
                try:
                    if await cand.is_visible(timeout=800) and await cand.is_enabled(timeout=800):
                        return cand
                except Exception:
                    continue
        return None

    async def fill_textbox(self, text: str, purpose: str = "generic") -> None:
        textbox = await self.find_textbox(purpose)
        if textbox is None:
            await self.debug_dump(f"{purpose}_composer_not_found")
            raise ToolError(ERROR_COMPOSER_NOT_FOUND, f"未找到可输入的{purpose}文本框。")
        await textbox.click(timeout=self.opts.timeout_ms)
        # Ctrl+A may not work in all contenteditable; clear through JS when possible.
        try:
            await textbox.evaluate("el => { if ('value' in el) el.value = ''; else el.innerText = ''; }")
        except Exception:
            pass
        try:
            await textbox.fill(text, timeout=3000)
        except Exception:
            await textbox.click()
            await textbox.press("Control+A")
            await textbox.press("Backspace")
            await textbox.type(text, delay=10)
        await asyncio.sleep(0.5)

    async def click_button_by_text(self, labels: List[str], *, timeout_ms: int = 8000) -> bool:
        page = await self.ensure_page()
        selectors = []
        for label in labels:
            selectors.extend([
                f'button:has-text("{label}")',
                f'[role="button"]:has-text("{label}")',
                f'text="{label}"',
            ])
        deadline = time.time() + timeout_ms / 1000
        last_exc = None
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = page.locator(sel).last
                    if await loc.count() > 0 and await loc.is_visible(timeout=500):
                        await loc.click(timeout=1500)
                        await asyncio.sleep(1.2)
                        await self.detect_blockers()
                        return True
                except Exception as exc:
                    last_exc = exc
                    continue
            await asyncio.sleep(0.5)
        if last_exc:
            eprint(f"click_button_by_text failed last_exc={type(last_exc).__name__}: {last_exc}")
        return False

    async def wait_for_publish_signal(self, original_text: str, seconds: int = 12) -> str:
        """轮询判断写操作的发送结果，返回 "success" / "failure" / "unknown"。

        判断依据（按优先级）：
          1) 页面上出现明确的成功 toast：发布成功 / 评论成功 / 回复成功 / 转发成功 / 已发送 / 已发布
          2) 页面上出现明确的失败 toast：发送/发布/评论/回复失败 / 操作过于频繁 / 稍后再试
          3) 文本框被清空（且原本有内容）→ 强阳性信号，视为 success
          4) 超时无明确信号 → unknown，由调用方决定如何处理（不要自动判失败）

        旧版只看 toast 文本，但微博桌面版 toast 经常一闪而过，错过就只能 unknown。
        加上"textbox cleared"信号后，绝大多数 unknown 会被正确判成 success。
        """
        page = await self.ensure_page()
        deadline = time.time() + seconds
        original_stripped = (original_text or "").strip()
        last_textbox_value: Optional[str] = None
        textbox_was_filled = False
        while time.time() < deadline:
            await self.detect_blockers()
            # 1) toast 检测
            try:
                body = await page.locator("body").inner_text(timeout=1000)
                success_phrases = ("发布成功", "评论成功", "转发成功", "回复成功", "已发送", "已发布", "发表成功")
                fail_phrases = ("发送失败", "发布失败", "评论失败", "回复失败", "转发失败", "操作过于频繁", "操作太频繁", "请稍后再试", "稍后再试")
                if any(s in body for s in success_phrases):
                    return "success"
                if any(s in body for s in fail_phrases):
                    return "failure"
            except Exception:
                pass
            # 2) textbox 清空检测：只要原本写过东西、现在变空，就是强阳性信号
            if original_stripped:
                try:
                    for sel in (
                        '[contenteditable="true"][role="textbox"]',
                        'div[contenteditable="true"]',
                        'textarea',
                    ):
                        loc = page.locator(sel)
                        cnt = 0
                        try:
                            cnt = await loc.count()
                        except Exception:
                            cnt = 0
                        if cnt == 0:
                            continue
                        # 取第一个可见的
                        for i in range(min(cnt, 6)):
                            cand = loc.nth(i)
                            try:
                                if not await cand.is_visible(timeout=300):
                                    continue
                            except Exception:
                                continue
                            cur_val = ""
                            try:
                                cur_val = (await cand.input_value(timeout=300)) or ""
                            except Exception:
                                try:
                                    cur_val = (await cand.inner_text(timeout=300)) or ""
                                except Exception:
                                    cur_val = ""
                            cur_val = cur_val.strip()
                            # 第一次见到框里有跟我们提交内容相符的文字 → 记下"被填过"
                            if not textbox_was_filled and cur_val and (
                                cur_val == original_stripped
                                or original_stripped[:20] in cur_val
                                or cur_val[:20] in original_stripped
                            ):
                                textbox_was_filled = True
                                last_textbox_value = cur_val
                                break
                            # 之前框里有我们写的东西，现在变空 → 已提交
                            if textbox_was_filled and cur_val == "":
                                # 给 toast 一点出现的时间
                                await asyncio.sleep(0.6)
                                try:
                                    body2 = await page.locator("body").inner_text(timeout=500)
                                    if any(s in body2 for s in fail_phrases):
                                        return "failure"
                                except Exception:
                                    pass
                                return "success"
                            last_textbox_value = cur_val
                            break
                        if textbox_was_filled:
                            break
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        return "unknown"

    async def wait_for_text_disappear_or_success(self, text: str, seconds: int = 12) -> str:
        """向后兼容包装：返回 wait_for_publish_signal 的结果，但若是明确 failure
        则保持旧版的"raise PUBLISH_FAILED"语义。"""
        signal = await self.wait_for_publish_signal(text, seconds=seconds)
        if signal == "failure":
            raise ToolError(ERROR_PUBLISH_FAILED, "微博页面提示发送/发布失败或操作频繁。")
        return signal

    async def post_status(self, text: str, visibility: str = "public", images: Optional[List[str]] = None) -> dict:
        await self.require_login()
        if images:
            raise ToolError(ERROR_PUBLISH_FAILED, "当前桌面版第一版尚未实现图片上传；请先发纯文本，或后续扩展 upload-image。")
        await self.goto(DESKTOP_HOME)
        await self.fill_textbox(text, purpose="post")
        clicked = await self.click_button_by_text(["发布", "发送"])
        if not clicked:
            await self.debug_dump("post_button_not_found")
            raise ToolError(ERROR_PUBLISH_FAILED, "未找到桌面网页版发布按钮。")
        signal = await self.wait_for_publish_signal(text, seconds=12)
        if signal == "failure":
            await self.debug_dump("post_publish_failed", {"text": text})
            raise ToolError(ERROR_PUBLISH_FAILED, "微博页面提示发布失败或操作频繁。")
        # 历史教训：发完帖立刻去自己 timeline 抓往往抓不到（缓存/页面未刷新/最近微博列表是
        # SPA 状态没更新）。如果 UI 已经给出 success 信号或 textbox 已被清空，就信它，
        # 不再硬性要求能在自己微博列表里 fetch 到。fetch 只作为补强，找不到也只是软警告。
        if signal == "success":
            await asyncio.sleep(1)
            match = None
            try:
                items = (await self.stats(mine=True, limit=10)).get("items", [])
                snippet = text.strip()[:40]
                match = next((it for it in items if snippet and snippet in (it.get("text") or "")), None)
            except Exception:
                match = None
            return {
                "post": match or {"text": text},
                "verified": True,
                "verification": "ui_signal" if not match else "ui_signal+fetch",
            }
        # signal == "unknown": 软失败 fallback
        await asyncio.sleep(2)
        items: List[dict] = []
        verify_error: Optional[str] = None
        try:
            items = (await self.stats(mine=True, limit=10)).get("items", [])
        except Exception as exc:
            verify_error = f"{type(exc).__name__}: {exc}"
            try:
                await self.goto(DESKTOP_HOME)
                items = await self.extract_cards(limit=10)
            except Exception as exc2:
                verify_error = f"{verify_error}; {type(exc2).__name__}: {exc2}"
                items = []
        snippet = text.strip()[:40]
        match = next((it for it in items if snippet and snippet in (it.get("text") or "")), None)
        warnings: List[str] = []
        if not match:
            warnings.append(
                "未能在自己的近期微博中匹配到该发布内容，且页面没有明确的失败提示。"
                "桌面版发布后页面通常需要刷新才会显示新微博，多数情况下已发布成功。请到自己主页确认。"
            )
            if verify_error:
                warnings.append(f"自己微博抓取出错：{verify_error}")
            await self.debug_dump("post_unverified", {"text": text, "recent_items": items[:5], "verify_error": verify_error})
        return {
            "post": match or {"text": text},
            "verified": bool(match),
            "verification": "fetch" if match else "unverified",
            "warnings": warnings,
        }

    async def comment_status(self, post_url_or_id: str, text: str) -> dict:
        await self.require_login()
        url = post_url_or_id if post_url_or_id.startswith("http") else f"https://weibo.com/detail/{parse_weibo_id(post_url_or_id)}"
        await self.goto(url)
        # Open/focus comment area.
        await self.click_button_by_text(["评论"], timeout_ms=3000)
        await self.fill_textbox(text, purpose="comment")
        clicked = await self.click_button_by_text(["评论", "发布", "发送"])
        if not clicked:
            await self.debug_dump("comment_button_not_found")
            raise ToolError(ERROR_PUBLISH_FAILED, "未找到评论发布按钮。")
        signal = await self.wait_for_publish_signal(text, seconds=12)
        if signal == "failure":
            await self.debug_dump("comment_publish_failed", {"post_url": url, "text": text})
            raise ToolError(ERROR_PUBLISH_FAILED, "微博页面提示评论失败或操作频繁。")
        # 历史教训：微博评论提交后并不一定会出现在评论区列表的可见位置（按热度排序、
        # 分页、机审延迟、对方关注限制等都会让我们的评论暂时看不见），但它已经发出去了。
        # 因此：UI 信号已经是 success/textbox-cleared 时直接信，不再做硬性 fetch 校验；
        # 信号 unknown 时尝试 fetch 校验作为补强，找不到也只是降级为 verified=false +
        # 一条 warning，绝不再 raise VERIFY_FAILED。
        if signal == "success":
            return {
                "comment": {"post_url": url, "text": text},
                "verified": True,
                "verification": "ui_signal",
            }
        # signal == "unknown": 尝试 fetch 校验，软失败
        await asyncio.sleep(2)
        verified = False
        verify_error: Optional[str] = None
        try:
            detail = await self.post_detail(url, include_comments=True, limit_comments=60)
            verified = any(text.strip()[:30] in (c.get("text") or "") for c in detail.get("comments", []))
        except Exception as exc:
            verify_error = f"{type(exc).__name__}: {exc}"
        warnings: List[str] = []
        verification = "fetch" if verified else "unverified"
        if not verified:
            warnings.append(
                "未能在评论区找到该评论，且页面没有明确的失败提示。微博评论按热度排序、分页或机审延迟都可能造成短时不可见，多数情况下评论已经发出。"
                "如需确认请直接打开微博详情页查看。"
            )
            if verify_error:
                warnings.append(f"评论区抓取也失败了：{verify_error}")
            await self.debug_dump("comment_unverified", {"post_url": url, "text": text, "verify_error": verify_error})
        return {
            "comment": {"post_url": url, "text": text},
            "verified": verified,
            "verification": verification,
            "warnings": warnings,
        }

    async def reply_comment(self, comment_id: str, text: str, post_url: Optional[str] = None, comment_text: Optional[str] = None) -> dict:
        await self.require_login()
        if comment_id.startswith("http") and not post_url:
            post_url = comment_id
        if not post_url:
            raise ToolError(ERROR_TARGET_NOT_FOUND, "桌面网页版回复评论需要 --post-url 或把 comment_id 传为评论所在 URL；只有裸 comment-id 不足以导航。", status="blocked", blocked_reason="target_not_found")
        await self.goto(post_url)
        await self.scroll_page(rounds=2)
        page = await self.ensure_page()
        # Try to click reply within the matching comment container.
        clicked_reply = False
        if comment_text:
            loc = page.locator(f'text="{comment_text[:30]}"').first
            try:
                if await loc.count() > 0:
                    handle = await loc.element_handle()
                    if handle:
                        await handle.evaluate(r"""
                        el => {
                          let n = el;
                          for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
                            const btns = Array.from(n.querySelectorAll('button,[role="button"],a,span'));
                            const b = btns.find(x => /回复|评论/.test((x.innerText || '').trim()));
                            if (b) { b.click(); return true; }
                          }
                          return false;
                        }
                        """)
                        clicked_reply = True
                        await asyncio.sleep(1)
            except Exception:
                clicked_reply = False
        if not clicked_reply:
            clicked_reply = await self.click_button_by_text(["回复"], timeout_ms=5000)
        if not clicked_reply:
            await self.debug_dump("reply_button_not_found")
            raise ToolError(ERROR_SELECTOR, "未找到评论回复按钮；请提供 --comment-text 或用 debug 文件修选择器。")
        await self.fill_textbox(text, purpose="comment")
        clicked = await self.click_button_by_text(["回复", "评论", "发布", "发送"])
        if not clicked:
            await self.debug_dump("reply_send_button_not_found")
            raise ToolError(ERROR_PUBLISH_FAILED, "未找到回复发送按钮。")
        signal = await self.wait_for_publish_signal(text, seconds=12)
        if signal == "failure":
            await self.debug_dump("reply_publish_failed", {"post_url": post_url, "comment_id": comment_id, "text": text})
            raise ToolError(ERROR_PUBLISH_FAILED, "微博页面提示回复失败或操作频繁。")
        verified = (signal == "success")
        warnings: List[str] = []
        if not verified:
            warnings.append(
                "回复已发送但页面没有明确的成功提示。回复在桌面版往往不会立即显示，多数情况下已发出。"
            )
        return {
            "reply": {"post_url": post_url, "comment_id": comment_id, "text": text},
            "verified": verified,
            "verification": "ui_signal" if verified else "unverified",
            "warnings": warnings,
        }

    async def repost_status(self, post_url_or_id: str, text: str) -> dict:
        await self.require_login()
        url = post_url_or_id if post_url_or_id.startswith("http") else f"https://weibo.com/detail/{parse_weibo_id(post_url_or_id)}"
        await self.goto(url)
        clicked = await self.click_button_by_text(["转发"], timeout_ms=6000)
        if not clicked:
            await self.debug_dump("repost_button_not_found")
            raise ToolError(ERROR_SELECTOR, "未找到转发按钮。")
        await self.fill_textbox(text or "转发微博", purpose="generic")
        clicked2 = await self.click_button_by_text(["转发", "发布", "发送"])
        if not clicked2:
            await self.debug_dump("repost_send_button_not_found")
            raise ToolError(ERROR_PUBLISH_FAILED, "未找到转发发送按钮。")
        signal = await self.wait_for_publish_signal(text or "转发微博", seconds=12)
        if signal == "failure":
            await self.debug_dump("repost_publish_failed", {"post_url": url, "text": text})
            raise ToolError(ERROR_PUBLISH_FAILED, "微博页面提示转发失败或操作频繁。")
        verified = (signal == "success")
        warnings: List[str] = []
        if not verified:
            warnings.append("转发已发送但页面没有明确的成功提示，多数情况下已发出。")
        return {
            "post_url": url,
            "reposted": True,
            "text": text,
            "verified": verified,
            "verification": "ui_signal" if verified else "unverified",
            "warnings": warnings,
        }

    async def like_status(self, post_url_or_id: str, unlike: bool = False) -> dict:
        """点赞 / 取消点赞。

        历史教训：微博桌面网页版的"赞"按钮是 SVG icon 按钮，本身没有可见文本"赞"，
        旁边的"赞"或数字是 *计数* span，不是按钮。所以 `text="赞"` 一类的选择器
        要么找不到，要么误点到不可点击的计数。这里改用 JS 端综合判定：
          1) aria-label 包含"赞 / 点赞 / 取消赞 / 已赞"
          2) class 名包含 woo-like / like-btn / likeIcon 等微博常用类名指纹
          3) 元素位于 ToolBar / Footer / Action 类容器内，得分最高
          4) 通过 aria-pressed / class 包含 active|liked / 文本含"已赞|取消赞"判断
             当前是否已点赞，避免 unlike 时点到未点赞的项、或 like 时再点一次取消。
        """
        await self.require_login()
        url = post_url_or_id if post_url_or_id.startswith("http") else f"https://weibo.com/detail/{parse_weibo_id(post_url_or_id)}"
        await self.goto(url)
        page = await self.ensure_page()

        js = r"""
        (unlike) => {
          function visible(el) {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
          }
          function getCls(el) {
            const c = el && el.className;
            if (!c) return '';
            if (typeof c === 'string') return c;
            if (typeof c.baseVal === 'string') return c.baseVal; // SVGAnimatedString
            return '';
          }
          function likeStateOf(el) {
            const ariaPressed = (el.getAttribute && el.getAttribute('aria-pressed')) || '';
            if (ariaPressed === 'true') return 'liked';
            // 看自己和最近 4 层祖先的 class 是否有 active/liked/woo-like-on 标记
            let n = el; let depth = 0;
            while (n && depth < 5) {
              const cls = getCls(n);
              if (/(\b|_)(active|liked|woo-like-on|on)(\b|_)/i.test(cls)) return 'liked';
              n = n.parentElement; depth++;
            }
            // 文本/aria 包含已赞 / 取消赞
            const t = (el.innerText || '') + ' ' + ((el.getAttribute && el.getAttribute('aria-label')) || '');
            if (/取消赞|已赞|已点赞/.test(t)) return 'liked';
            return 'unliked';
          }
          // 候选元素：button、role=button、woo-like* / like* class 容器
          const seen = new Set();
          const candidates = [];
          function add(el) {
            if (!el || seen.has(el)) return;
            seen.add(el);
            candidates.push(el);
          }
          for (const el of document.querySelectorAll('button, [role="button"]')) add(el);
          for (const el of document.querySelectorAll(
            '[class*="woo-like"], [class*="LikeBtn"], [class*="likeBtn"], [class*="like-btn"], [class*="LikeIcon"], [class*="likeIcon"]'
          )) add(el);
          // 也把带"赞|点赞"文本的 a/span 拉进来——有些主题里点赞按钮是 a
          for (const el of document.querySelectorAll('a, span')) {
            const t = (el.innerText || '').trim();
            const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
            if (/^(赞|点赞|取消赞|已赞)$/.test(t) || /(^|\s)(点赞|取消赞|已赞)(\s|$)/.test(aria)) add(el);
          }

          const scored = [];
          for (const el of candidates) {
            if (!visible(el)) continue;
            const aria = (el.getAttribute && el.getAttribute('aria-label')) || '';
            const text = (el.innerText || '').trim();
            const cls = getCls(el);

            // 是不是点赞相关元素？
            const isLikeByCls = /woo-like|like.?btn|likeIcon/i.test(cls);
            const isLikeByAria = /(^|\s)(赞|点赞|取消赞|已赞)(\s|$)/.test(aria);
            // 文本只允许 "赞" / "点赞" / "已赞" / "取消赞" / "赞 数字" 这种短文本，避免把
            // 微博正文里包含"赞"字的段落或导航菜单选中。
            const isLikeByText = /^(赞|点赞|取消赞|已赞)\s*[\d.,万亿]*$/.test(text);
            if (!(isLikeByCls || isLikeByAria || isLikeByText)) continue;

            // 排除明显的非操作元素：导航/菜单
            // 通过祖先的 class/role 判断
            let inToolbar = false, inNav = false, inFeedOrDetail = false;
            let n = el; let depth = 0;
            while (n && depth < 8) {
              const cc = getCls(n);
              const role = n.getAttribute && n.getAttribute('role');
              if (/ToolBar|toolbar|Footer|footer|Action|action|opt-box|operation/i.test(cc)) inToolbar = true;
              if (/Nav|nav|side-?bar|menu/i.test(cc) || role === 'navigation' || role === 'menu') inNav = true;
              if (/Feed|feed|Detail|detail|Card|card|Item|item/i.test(cc)) inFeedOrDetail = true;
              n = n.parentElement; depth++;
            }
            if (inNav && !inToolbar) continue;

            let score = 0;
            if (isLikeByAria) score += 4;
            if (isLikeByCls) score += 3;
            if (isLikeByText) score += 1;
            if (inToolbar) score += 3;
            if (inFeedOrDetail) score += 2;

            scored.push({el, score, state: likeStateOf(el), aria, text, cls: cls.slice(0, 80)});
          }
          scored.sort((a, b) => b.score - a.score);

          if (scored.length === 0) {
            return {ok: false, reason: 'no_candidates'};
          }
          const wantState = unlike ? 'liked' : 'unliked';
          const target = scored.find(x => x.state === wantState) || scored[0];
          if (target.state !== wantState) {
            return {ok: false, reason: 'already_in_desired_state', current_state: target.state};
          }
          try { target.el.scrollIntoView({block: 'center', behavior: 'instant'}); } catch (e) {}
          target.el.click();
          return {
            ok: true,
            state_before: target.state,
            picked: {aria: target.aria, text: target.text.slice(0, 40), cls: target.cls, score: target.score}
          };
        }
        """
        try:
            result = await page.evaluate(js, unlike)
        except Exception as exc:
            await self.debug_dump("like_eval_failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise ToolError(ERROR_SELECTOR, f"点赞按钮 JS 查找失败：{type(exc).__name__}: {exc}")

        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "already_in_desired_state":
                return {
                    "post_url": url,
                    "liked": not unlike,
                    "noop": True,
                    "note": f"已经处于目标状态（{result.get('current_state')}），无需点击。",
                }
            # 兜底：旧的 click_button_by_text 路径
            clicked = await self.click_button_by_text(
                ["取消赞", "已赞"] if unlike else ["赞", "点赞"],
                timeout_ms=3000,
            )
            if not clicked:
                await self.debug_dump("like_button_not_found", {"reason": reason})
                raise ToolError(
                    ERROR_SELECTOR,
                    f"未找到点赞/取消点赞按钮（reason={reason}）。微博桌面版可能改版，请用 --debug 抓 HTML 修选择器。",
                )
        await asyncio.sleep(1.2)
        try:
            await self.detect_blockers()
        except ToolError:
            raise
        except Exception:
            pass
        return {"post_url": url, "liked": not unlike, "picked": result.get("picked")}

    async def delete_post(self, post_url_or_id: str) -> dict:
        await self.require_login()
        url = post_url_or_id if post_url_or_id.startswith("http") else f"https://weibo.com/detail/{parse_weibo_id(post_url_or_id)}"
        await self.goto(url)
        clicked = await self.click_button_by_text(["更多", "···", "..."], timeout_ms=5000)
        if not clicked:
            # Try keyboard/context fallback is not safe enough for delete.
            raise ToolError(ERROR_SELECTOR, "未找到更多菜单，无法执行删除。")
        await asyncio.sleep(0.8)
        clicked2 = await self.click_button_by_text(["删除"], timeout_ms=5000)
        if not clicked2:
            raise ToolError(ERROR_SELECTOR, "更多菜单里未找到删除。")
        await asyncio.sleep(0.5)
        # Confirm dialog.
        await self.click_button_by_text(["确定", "确认", "删除"], timeout_ms=5000)
        return {"post_url": url, "deleted": True, "verified": None}

    async def delete_comment(self, comment_id: str, post_url: Optional[str] = None, comment_text: Optional[str] = None) -> dict:
        await self.require_login()
        if not post_url:
            raise ToolError(ERROR_TARGET_NOT_FOUND, "桌面网页版删除评论需要 --post-url 和最好提供 --comment-text 定位。", status="blocked", blocked_reason="target_not_found")
        await self.goto(post_url)
        await self.scroll_page(rounds=2)
        page = await self.ensure_page()
        if comment_text:
            try:
                loc = page.locator(f'text="{comment_text[:30]}"').first
                if await loc.count() > 0:
                    await loc.click()
            except Exception:
                pass
        await self.click_button_by_text(["更多", "···", "..."], timeout_ms=5000)
        clicked = await self.click_button_by_text(["删除"], timeout_ms=5000)
        if not clicked:
            raise ToolError(ERROR_SELECTOR, "未找到删除评论入口。")
        await self.click_button_by_text(["确定", "确认", "删除"], timeout_ms=5000)
        return {"comment_id": comment_id, "post_url": post_url, "deleted": True, "verified": None}


def ok_response(action: str, *, status: str = "done", data: Optional[dict] = None, items: Optional[list] = None, evidence: Optional[list] = None, debug_artifacts: Optional[list] = None, warnings: Optional[list] = None) -> dict:
    return {
        "ok": True,
        "action": action,
        "status": status,
        "collected_at": utc_now_iso(),
        "data": data or {},
        "items": items or [],
        "evidence": evidence or [],
        "warnings": warnings or [],
        "next_suggested_action": None,
        "debug_artifacts": debug_artifacts or [],
    }


def error_response(action: str, exc: Exception, debug_artifacts: Optional[list] = None) -> dict:
    if isinstance(exc, ToolError):
        return {
            "ok": False,
            "action": action,
            "status": exc.status,
            "error_code": exc.error_code,
            "message": exc.message,
            "blocked_reason": exc.blocked_reason,
            "data": exc.data,
            "debug_artifacts": debug_artifacts or [],
            "collected_at": utc_now_iso(),
        }
    return {
        "ok": False,
        "action": action,
        "status": "failed",
        "error_code": ERROR_UNKNOWN,
        "message": f"{type(exc).__name__}: {exc}",
        "blocked_reason": None,
        "data": {"traceback": traceback.format_exc()[-4000:]},
        "debug_artifacts": debug_artifacts or [],
        "collected_at": utc_now_iso(),
    }


async def cmd_health(args, opts: RuntimeOptions) -> dict:
    data: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "playwright_importable": async_playwright is not None,
        "profile": str(Path(opts.profile).expanduser()),
        "site": "desktop_weibo_com",
        "desktop_home": DESKTOP_HOME,
    }
    if async_playwright is None:
        return error_response("health", ToolError(ERROR_DEP, "缺少 playwright。请安装 requirements 并执行 playwright install chromium。", status="blocked", blocked_reason="missing_dependency"))
    if getattr(args, "no_browser", False):
        return ok_response("health", data=data)
    async with WeiboSession(opts) as s:
        await s.goto(DESKTOP_HOME)
        data["url"] = (await s.ensure_page()).url
        data["cookies"] = await s.cookies_summary()
        data["login_state"] = "logged_in" if await s.is_logged_in() else "not_logged_in"
        return ok_response("health", data=data, debug_artifacts=s.debug_artifacts)


async def cmd_login(args, opts: RuntimeOptions) -> dict:
    # Login is a special interactive mode. It MUST keep Chromium alive so the human
    # can connect through ssh -L + chrome://inspect and handle login/captcha/security
    # pages manually. Do NOT call s.goto(), because s.goto() intentionally detects
    # blockers and would exit exactly when the human needs to take over.
    opts.headless = not getattr(args, "headful", False)
    async with WeiboSession(opts) as s:
        page = await s.ensure_page()
        try:
            await page.goto(DESKTOP_HOME, wait_until="domcontentloaded", timeout=opts.timeout_ms)
        except PWTimeout:
            pass
        except Exception as exc:
            # Keep the browser alive even if the initial navigation hits a transient
            # network/security page. The user can still inspect and navigate manually.
            eprint(f"初始打开微博页面失败但浏览器仍保持运行：{type(exc).__name__}: {exc}")

        if opts.headless:
            eprint("微博桌面网页版已在 VM 的无头 Chromium 中保持打开。")
            eprint("请在本地使用 ssh -L 转发端口，然后用 chrome://inspect 连接进去手动登录/处理验证码/安全提示。")
        else:
            eprint("微博桌面网页版登录窗口已打开。请完成登录/验证码/安全确认。")
        if opts.cdp_port:
            eprint(f"远程调试端口：{opts.cdp_port}")
            eprint(f"本地命令示例：ssh -L {opts.cdp_port}:127.0.0.1:{opts.cdp_port} zhou@192.168.122.102")
            eprint("本地浏览器打开：chrome://inspect/#devices，Configure 添加 localhost:%s" % opts.cdp_port)
        eprint("重要：这个 login 命令会一直运行。完成登录后，回到此终端按 Ctrl+C 保存 profile 并退出。")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _stop(*_):
            stop.set()

        try:
            loop.add_signal_handler(signal.SIGINT, _stop)
            loop.add_signal_handler(signal.SIGTERM, _stop)
        except (NotImplementedError, RuntimeError):
            pass

        while not stop.is_set():
            await asyncio.sleep(1)

        # 尝试读 cookies 做最后的状态汇报。注意：通过 chrome://inspect 操作时
        # 用户可能已经关掉了页面/窗口，或浏览器驱动连接已断开，这里读 cookies
        # 失败不应该让 login 命令整体 fail —— profile 是在 __aexit__ 里
        # context.close() 时持久化到磁盘的，与 cookies_summary 无关。
        summary: dict
        summary_error: Optional[str] = None
        try:
            summary = await s.cookies_summary()
        except Exception as exc:
            summary_error = f"{type(exc).__name__}: {exc}"
            eprint(f"读取 cookies 摘要失败但 profile 仍会随浏览器关闭一起保存：{summary_error}")
            summary = {"likely_logged_in": False, "cookie_names": []}
        data_payload = {
            "profile": opts.profile,
            "cookies": summary,
            "login_state": "logged_in" if summary.get("likely_logged_in") else "unknown",
            "note": "login 命令已按用户中断保存 profile。若 login_state 不是 logged_in，请运行 `whoami --json` 验证；若仍未登录，请重新运行 login 并在 chrome://inspect 中完成登录。",
        }
        if summary_error:
            data_payload["cookies_summary_error"] = summary_error
        return ok_response(
            "login",
            status="profile_saved",
            data=data_payload,
            debug_artifacts=s.debug_artifacts,
        )


async def run_action(args, opts: RuntimeOptions) -> dict:
    action = args.action
    policy = load_policy(opts.policy)
    if action in WRITE_ACTIONS and not policy_allowed(policy, action):
        return error_response(action, ToolError(ERROR_POLICY_DENIED, f"policy 不允许执行 {action}", status="blocked", blocked_reason="policy_denied", data={"policy": opts.policy}))

    async with WeiboSession(opts) as s:
        if action == "whoami":
            data = await s.whoami()
            return ok_response(action, data=data, debug_artifacts=s.debug_artifacts)
        if action == "feed":
            data = await s.feed(kind=args.kind, limit=args.limit)
            return ok_response(action, data={"kind": args.kind}, items=data.get("items", []), warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "search":
            data = await s.search(query=args.query, sort=args.sort, limit=args.limit)
            return ok_response(action, data={"query": args.query, "sort": args.sort}, items=data.get("items", []), warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "user":
            data = await s.user(url_or_uid=args.url or args.uid, limit=args.limit)
            return ok_response(action, data={"user": data.get("user")}, items=data.get("items", []), warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "post-detail":
            data = await s.post_detail(args.url or args.id, include_comments=args.include in {"comments", "all"}, limit_comments=args.limit_comments)
            return ok_response(action, data={"post": data.get("post"), "comments": data.get("comments", [])}, warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "notifications":
            data = await s.notifications(kind=args.kind, limit=args.limit)
            return ok_response(action, data={"kind": args.kind}, items=data.get("items", []), warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "stats":
            data = await s.stats(mine=args.mine, post_url=args.post_url, limit=args.limit)
            return ok_response(action, data={}, items=data.get("items", []), warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)

        # Write actions: dedupe by action+target+text hash to avoid accidental double-submit.
        def _publish_status_for(data: dict) -> str:
            """已发送 = published；发送了但没拿到强校验信号 = published_unverified。
            Nova 拿到 published_unverified 时不要当作失败，按用户口径"评论失败也不用管"
            处理；但区分出来便于追溯。"""
            if data.get("verified") is False:
                return "published_unverified"
            return "published"

        if action == "post":
            text = read_text_file(args.text_file)
            dedupe = f"post:{sha256_text(text)}"
            dup = recent_duplicate(opts, dedupe)
            if dup:
                return ok_response(action, status="duplicate_skipped", data={"duplicate_of": dup}, debug_artifacts=s.debug_artifacts)
            data = await s.post_status(text, visibility=args.visibility, images=args.images)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": dedupe, "result": data})
            return ok_response(action, status=_publish_status_for(data), data=data, warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "comment":
            text = read_text_file(args.text_file)
            target = args.post_url or args.id
            dedupe = f"comment:{target}:{sha256_text(text)}"
            dup = recent_duplicate(opts, dedupe)
            if dup:
                return ok_response(action, status="duplicate_skipped", data={"duplicate_of": dup}, debug_artifacts=s.debug_artifacts)
            data = await s.comment_status(target, text)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": dedupe, "result": data})
            return ok_response(action, status=_publish_status_for(data), data=data, warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "reply-comment":
            text = read_text_file(args.text_file)
            data = await s.reply_comment(args.comment_id, text, post_url=args.post_url, comment_text=args.comment_text)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": f"reply:{args.comment_id}:{sha256_text(text)}", "result": data})
            return ok_response(action, status=_publish_status_for(data), data=data, warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "repost":
            text = read_text_file(args.text_file) if args.text_file else "转发微博"
            target = args.post_url or args.id
            data = await s.repost_status(target, text)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": f"repost:{target}:{sha256_text(text)}", "result": data})
            return ok_response(action, status=_publish_status_for(data), data=data, warnings=data.get("warnings", []), debug_artifacts=s.debug_artifacts)
        if action == "like":
            target = args.post_url or args.id
            data = await s.like_status(target, unlike=False)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": f"like:{target}", "result": data})
            return ok_response(action, data=data, debug_artifacts=s.debug_artifacts)
        if action == "unlike":
            target = args.post_url or args.id
            data = await s.like_status(target, unlike=True)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": f"unlike:{target}", "result": data})
            return ok_response(action, data=data, debug_artifacts=s.debug_artifacts)
        if action == "delete-post":
            target = args.post_url or args.id
            data = await s.delete_post(target)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": f"delete-post:{target}", "result": data})
            return ok_response(action, data=data, debug_artifacts=s.debug_artifacts)
        if action == "delete-comment":
            data = await s.delete_comment(args.comment_id, post_url=args.post_url, comment_text=args.comment_text)
            append_action_log(opts, {"epoch": time.time(), "action": action, "ok": True, "dedupe_key": f"delete-comment:{args.comment_id}", "result": data})
            return ok_response(action, data=data, debug_artifacts=s.debug_artifacts)

    raise ToolError(ERROR_UNKNOWN, f"未知 action: {action}")


def add_common(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--profile", default=(argparse.SUPPRESS if suppress_defaults else DEFAULT_PROFILE), help="Chromium 持久化 profile 目录。")
    parser.add_argument("--state-dir", default=(argparse.SUPPRESS if suppress_defaults else DEFAULT_STATE_DIR), help="日志/debug 状态目录。")
    parser.add_argument("--policy", default=(argparse.SUPPRESS if suppress_defaults else DEFAULT_POLICY), help="微博授权 policy JSON。")
    parser.add_argument("--headful", action="store_true", default=(argparse.SUPPRESS if suppress_defaults else False), help="非登录命令也显示浏览器窗口。")
    parser.add_argument("--timeout-ms", type=int, default=(argparse.SUPPRESS if suppress_defaults else 45_000), help="Playwright 默认超时。")
    parser.add_argument("--debug", action="store_true", default=(argparse.SUPPRESS if suppress_defaults else False), help="保存截图/HTML/debug JSON。")
    parser.add_argument("--json", action="store_true", default=(argparse.SUPPRESS if suppress_defaults else False), help="stdout 输出 JSON。")

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="nova 微博桌面网页版自动化工具", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_common(ap)
    sub = ap.add_subparsers(dest="action", required=True)

    def addp(name: str, **kwargs):
        p = sub.add_parser(name, **kwargs)
        # Also allow common args after the subcommand, because nova examples naturally put --json at the end.
        add_common(p, suppress_defaults=True)
        return p

    p = addp("health", help="检查依赖、浏览器、profile 和登录态。")
    p.add_argument("--no-browser", action="store_true", help="只检查 Python 依赖，不启动浏览器。")

    p = addp("login", help="打开微博桌面网页版，让人类手动登录并保存 profile。默认无头运行，通过 chrome://inspect 操作。")
    p.add_argument("--cdp-port", type=int, default=9233, help="远程调试端口，便于 chrome://inspect。")

    addp("whoami", help="查看当前登录微博账号。")

    p = addp("feed", help="读取桌面网页版信息流。")
    p.add_argument("--kind", choices=["home", "following", "hot"], default="home")
    p.add_argument("--limit", type=int, default=20)

    p = addp("search", help="用微博桌面搜索读取微博。")
    p.add_argument("--query", required=True)
    p.add_argument("--sort", choices=["time", "hot"], default="time")
    p.add_argument("--limit", type=int, default=20)

    p = addp("user", help="读取用户主页。")
    p.add_argument("--url")
    p.add_argument("--uid")
    p.add_argument("--limit", type=int, default=20)

    p = addp("post-detail", help="读取单条微博详情和评论。")
    p.add_argument("--url")
    p.add_argument("--id")
    p.add_argument("--include", choices=["none", "comments", "all"], default="none")
    p.add_argument("--limit-comments", type=int, default=30)

    p = addp("notifications", help="读取微博通知页。")
    p.add_argument("--kind", choices=["comments", "mentions", "likes", "reposts", "all"], default="comments")
    p.add_argument("--limit", type=int, default=30)

    p = addp("stats", help="查看自己微博或单条微博的页面可见数据。")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--post-url")
    p.add_argument("--limit", type=int, default=20)

    p = addp("post", help="发纯文本微博。")
    p.add_argument("--text-file", required=True)
    p.add_argument("--visibility", choices=["public", "followers", "private"], default="public")
    p.add_argument("--images", nargs="*")

    p = addp("comment", help="评论一条微博。")
    p.add_argument("--post-url")
    p.add_argument("--id")
    p.add_argument("--text-file", required=True)

    p = addp("reply-comment", help="回复评论。桌面版建议提供 --post-url 和 --comment-text。")
    p.add_argument("--comment-id", required=True)
    p.add_argument("--post-url")
    p.add_argument("--comment-text")
    p.add_argument("--text-file", required=True)

    p = addp("repost", help="转发微博。")
    p.add_argument("--post-url")
    p.add_argument("--id")
    p.add_argument("--text-file")

    p = addp("like", help="点赞微博。")
    p.add_argument("--post-url")
    p.add_argument("--id")

    p = addp("unlike", help="取消点赞微博。")
    p.add_argument("--post-url")
    p.add_argument("--id")

    p = addp("delete-post", help="删除自己的微博。受 policy 控制。")
    p.add_argument("--post-url")
    p.add_argument("--id")

    p = addp("delete-comment", help="删除自己的评论。受 policy 控制。")
    p.add_argument("--comment-id", required=True)
    p.add_argument("--post-url")
    p.add_argument("--comment-text")

    return ap

def validate_args(args) -> None:
    if args.action == "user" and not (args.url or args.uid):
        raise ToolError(ERROR_TARGET_NOT_FOUND, "user 命令需要 --url 或 --uid")
    if args.action == "post-detail" and not (args.url or args.id):
        raise ToolError(ERROR_TARGET_NOT_FOUND, "post-detail 命令需要 --url 或 --id")
    if args.action in {"comment", "repost", "like", "unlike", "delete-post"} and not (getattr(args, "post_url", None) or getattr(args, "id", None)):
        raise ToolError(ERROR_TARGET_NOT_FOUND, f"{args.action} 命令需要 --post-url 或 --id")


async def amain() -> int:
    parser = build_parser()
    args = parser.parse_args()
    opts = RuntimeOptions(
        profile=args.profile,
        state_dir=args.state_dir,
        policy=args.policy,
        headless=not args.headful,
        cdp_port=getattr(args, "cdp_port", None),
        timeout_ms=args.timeout_ms,
        debug=args.debug,
        json_stdout=True,
    )
    try:
        validate_args(args)
        if args.action == "health":
            resp = await cmd_health(args, opts)
        elif args.action == "login":
            resp = await cmd_login(args, opts)
        else:
            resp = await run_action(args, opts)
    except Exception as exc:
        resp = error_response(getattr(args, "action", "unknown"), exc)
    print(json.dumps(resp, ensure_ascii=False, indent=None))
    return 0 if resp.get("ok") else 2


def main() -> None:
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        # login handles Ctrl+C itself; this is a fallback.
        print(json.dumps(error_response("unknown", ToolError(ERROR_UNKNOWN, "用户中断。")), ensure_ascii=False))
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
