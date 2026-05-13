# NOVA 微博桌面网页版工具使用手册

> 这份文档是写给 nova 自己看的。微博工具使用 `https://weibo.com` 桌面网页版，不默认使用 `m.weibo.cn`。nova 不要直接操作微博 DOM，而是通过固定 CLI 命令调用 `weibo_tool.py`，读取 JSON 返回值，然后继续行动。

---

## 0. 最高原则

nova 不直接研究微博网页。nova 只调用：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py ... --json
```

stdout 永远是一个 JSON 对象。nova 以 JSON 为准，不以浏览器页面猜测结果。

复杂的 Playwright、选择器、滚动、弹窗、验证发布成功，都属于 `weibo_tool.py`，不属于 nova。

---

## 1. 固定路径

```bash
~/nova_workspace/scripts/weibo/weibo_tool.py
~/nova_workspace/notes/NOVA_WEIBO_TOOL_GUIDE.md
~/nova_workspace/config/weibo_policy.json
~/nova_workspace/state/weibo/
~/.nova_profiles/weibo-default/
```

默认网站：

```text
https://weibo.com
```

不要改用 `m.weibo.cn`，除非用户以后明确要求。

---

## 2. 授权模型

用户授权 nova 在登录后的微博账号上独立完成：

- 看首页、热门、搜索、用户主页、通知、评论区。
- 发微博。
- 写评论。
- 回复评论。
- 点赞、取消点赞。
- 转发。
- 查看自己微博的页面可见浏览量、点赞数、评论数、转发数。

必须停止并报告的情况：

1. 需要登录、验证码、短信、人脸、账号安全确认。
2. 目标微博、目标用户、目标评论不明确。
3. 工具返回 `blocked`。
4. 连续两次同一命令失败。
5. 需要绕过验证码、绕过风控、刷屏、刷量、伪装或规避平台限制。
6. 删除类操作没有在 `weibo_policy.json` 中授权。

---

## 3. 每次微博任务先做 health

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py health --json
```

- `ok=true` 且 `data.login_state=logged_in`：继续。
- `login_state=not_logged_in` 或 `error_code=NEEDS_LOGIN`：需要人类登录。
- `error_code=CAPTCHA_REQUIRED`：需要人类处理验证码。
- `error_code=ACCOUNT_SECURITY_CHECK`：需要人类处理账号安全。
- `error_code=TOOL_MISSING_DEPENDENCY`：需要安装依赖。

不要跳过 health，不要无限重试。

---

## 4. 登录

登录命令：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py login \
  --profile ~/.nova_profiles/weibo-default \
  --cdp-port 9233
```

人类会通过桌面网页版完成登录和验证码。nova 不要尝试绕过验证码。

登录后确认：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py whoami --json
```

---

## 5. JSON 协议

成功：

```json
{
  "ok": true,
  "action": "feed",
  "status": "done",
  "data": {},
  "items": [],
  "warnings": [],
  "debug_artifacts": []
}
```

失败：

```json
{
  "ok": false,
  "action": "post",
  "status": "blocked",
  "error_code": "CAPTCHA_REQUIRED",
  "message": "微博出现验证码或安全验证，需要人类手动处理。",
  "blocked_reason": "captcha_required",
  "debug_artifacts": []
}
```

处理规则：

- `ok=true`：继续。
- `ok=false,status=blocked`：停止并告诉用户阻塞原因。
- `ok=false,status=failed`：同一命令最多重试 1 次；加 `--debug` 后仍失败就停止。
- `debug_artifacts` 非空时，保存路径给用户。

---

## 6. 读取命令

### 当前账号

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py whoami --json
```

### 首页

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py feed --kind home --limit 20 --json
```

### 热门/热搜页

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py feed --kind hot --limit 20 --json
```

### 搜索

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py search --query "关键词" --sort time --limit 20 --json
```

### 用户主页

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py user --url "https://weibo.com/u/UID" --limit 20 --json
```

### 单条微博详情

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py post-detail \
  --url "https://weibo.com/UID/BID" \
  --include comments \
  --limit-comments 50 \
  --json
```

### 通知

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py notifications --kind comments --limit 30 --json
```

`kind` 可选：`comments`、`mentions`、`likes`、`reposts`、`all`。

### 自己微博数据

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py stats --mine --limit 20 --json
```

单条微博：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py stats --post-url "https://weibo.com/UID/BID" --json
```

如果 `views=null`，说明桌面页面未显示或工具没取到，不能猜成 0。

---

## 7. 写入命令

### 发微博

```bash
cat > /tmp/weibo_post.txt <<'POST'
微博正文
POST
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py post --text-file /tmp/weibo_post.txt --json
```

带图发微博（最多 9 张，路径用空格分隔；支持 jpg/jpeg/png/gif/webp/bmp）：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py post \
  --text-file /tmp/weibo_post.txt \
  --images /path/to/a.jpg /path/to/b.png \
  --json
```

发布成功后，工具会尽量从自己的近期微博验证。如果返回 `VERIFY_FAILED`，不要假装成功。

### 评论

```bash
cat > /tmp/weibo_comment.txt <<'COMMENT'
评论正文
COMMENT
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py comment \
  --post-url "https://weibo.com/UID/BID" \
  --text-file /tmp/weibo_comment.txt \
  --json
```

带图评论（桌面网页版评论一次只能 1 张图）：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py comment \
  --post-url "https://weibo.com/UID/BID" \
  --text-file /tmp/weibo_comment.txt \
  --images /path/to/reaction.jpg \
  --json
```

评论前必须先 `post-detail` 确认目标微博。

### 回复评论

桌面网页版中，只有裸 `comment_id` 往往不足以定位。优先使用：

```bash
cat > /tmp/weibo_reply.txt <<'REPLY'
回复正文
REPLY
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py reply-comment \
  --post-url "https://weibo.com/UID/BID" \
  --comment-id "评论ID或占位ID" \
  --comment-text "对方评论中的一段文字" \
  --text-file /tmp/weibo_reply.txt \
  --json
```

带图回复（同样 1 张上限）：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py reply-comment \
  --post-url "https://weibo.com/UID/BID" \
  --comment-id "评论ID或占位ID" \
  --comment-text "对方评论中的一段文字" \
  --text-file /tmp/weibo_reply.txt \
  --images /path/to/reply.png \
  --json
```

如果没有 `post-url` 或无法定位评论，停止并说明目标不明确。

### 转发

```bash
cat > /tmp/weibo_repost.txt <<'REPOST'
转发正文
REPOST
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py repost \
  --post-url "https://weibo.com/UID/BID" \
  --text-file /tmp/weibo_repost.txt \
  --json
```

### 点赞 / 取消点赞

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py like --post-url "https://weibo.com/UID/BID" --json
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py unlike --post-url "https://weibo.com/UID/BID" --json
```

### 删除

删除是破坏性操作。只有 `weibo_policy.json` 允许时才能独立执行。

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py delete-post --post-url "https://weibo.com/UID/BID" --json
```

### 图片上传规则

`post` / `comment` / `reply-comment` 三个命令都接 `--images`，后面跟一个或多个本地图片路径，空格分隔：

```bash
--images /path/to/a.jpg /path/to/b.png
```

nova 需要记住：

- `post` 最多 **9 张**；`comment` 和 `reply-comment` 最多 **1 张**（桌面网页版评论本身只允许 1 张）。超出会得到 `error_code=IMAGE_INVALID`，不要重试。
- 支持的扩展名只有 `.jpg .jpeg .png .gif .webp .bmp`。其它扩展名会被 `IMAGE_INVALID` 拒绝。
- 单张图片超 20 MB 直接拒绝；超 5 MB 只是 warning。
- 图片路径必须 nova 自己能在文件系统上 `ls` 到。如果用户给了一个链接但没有下到本地，nova 不要自己上网下载，应该把这件事告诉用户。
- 在工具返回的 JSON 里多了一个 `images` 字段。`images.count` 是实际附加的张数，`images.thumbnails_detected` 是工具在撰写器附近观察到的缩略图数。如果二者不匹配，会有 warning，nova 应一并转告用户。
- 如果 `error_code=IMAGE_UPLOAD_FAILED`，意思是图片上传没成功（要么没拿到文件选择对话框，要么没有任何缩略图出现）。这种情况微博端通常什么都没发。把 `debug_artifacts` 路径告诉用户，让人去看是否被风控或图片本身有问题。

---

## 8. 日志要求

每次写操作后，nova 应把动作写入：

```bash
~/nova_workspace/journal/YYYY-MM-DD_weibo.md
```

格式：

```md
## 12:34 发微博

- 动作：post
- 站点：https://weibo.com
- 账号：...
- 链接：...
- 正文：...
- 工具返回：ok=true, status=published
```

---

## 9. Debug 和修复

失败时加 `--debug` 重跑一次，例如：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py feed --kind home --limit 10 --debug --json
```

debug 文件会在：

```bash
~/nova_workspace/state/weibo/debug/
```

如果微博桌面 DOM 变了，nova 不要自己改临时脚本。把 debug 路径报告给用户，由用户或开发者修 `weibo_tool.py`。

---

## 10. nova 的简化心智模型

微博能力就是这些命令：

- `feed`：看首页/热门。
- `search`：查话题。
- `user`：看用户主页。
- `post-detail`：看一条微博和评论。
- `notifications`：看通知。
- `stats`：看自己微博数据。
- `post`：发微博，可带 `--images`（最多 9 张）。
- `comment`：评论，可带 `--images`（最多 1 张）。
- `reply-comment`：回复评论，可带 `--images`（最多 1 张）。
- `repost`：转发。
- `like/unlike`：点赞/取消。

nova 只读手册、调 shell、读 JSON、写 journal。不要重新学习网页。
