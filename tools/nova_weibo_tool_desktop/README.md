# nova 微博桌面网页版工具包

这个包给 nova 增加微博操作能力，但**不修改 nova 本体代码**。nova 只需要通过 shell 调用：

```bash
~/nova_workspace/scripts/weibo/weibo_tool.py
```

本版明确使用 **微博桌面网页版 `https://weibo.com`**，不会默认使用 `m.weibo.cn`。登录态保存在独立 Chromium profile：

```bash
~/.nova_profiles/weibo-default/
```

这样不会要求你的手机微博退出，也不会拿手机端页面作为默认操作入口。

---

## 安装

在 VM 里解压后执行：

```bash
cd nova_weibo_tool_desktop
bash install/install_weibo_tool.sh
```

安装 Python 依赖：

```bash
cd ~/nova_workspace/scripts/weibo
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

检查依赖：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py health --no-browser --json
```

---

## 第一次登录

登录必须人工完成，其他操作可以交给 nova。

在 VM 里运行：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py login \
  --profile ~/.nova_profiles/weibo-default \
  --cdp-port 9233
```

如果 VM 没有图形界面，本地电脑开 SSH 隧道：

```bash
ssh -L 9233:127.0.0.1:9233 你的用户名@虚拟机IP
```

然后本地 Chrome / Edge 打开：

```text
chrome://inspect/#devices
```

点 `Configure...`，添加：

```text
localhost:9233
```

看到远程的 `https://weibo.com` 页面后点 `inspect`，在里面完成微博网页登录、验证码、短信或安全确认。完成后回到 VM 终端按 `Ctrl+C`，profile 会保存。

验证：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py health --json
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py whoami --json
```

---

## 给 nova 的一句话

让 nova 先读：

```bash
cat ~/nova_workspace/notes/NOVA_WEIBO_TOOL_GUIDE.md
```

然后你可以对 nova 说：

> 你已经获得我的微博账号总授权。登录、验证码、账号安全确认由我手动处理；除此之外，你需要按 `NOVA_WEIBO_TOOL_GUIDE.md` 调用 `weibo_tool.py`，通过微博桌面网页版 `https://weibo.com` 独立完成看微博、发微博、评论、回复评论、查看数据等任务。工具返回 blocked 时停止并说明原因。

---

## 常用命令

### 健康检查

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py health --json
```

### 当前账号

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py whoami --json
```

### 看首页

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py feed --kind home --limit 20 --json
```

### 看热搜/热门页

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py feed --kind hot --limit 20 --json
```

### 搜索微博

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py search --query "关键词" --sort time --limit 20 --json
```

### 看用户主页

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py user --url "https://weibo.com/u/用户UID" --limit 20 --json
```

### 看单条微博

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py post-detail --url "https://weibo.com/用户UID/微博BID" --include comments --limit-comments 50 --json
```

### 发微博

```bash
cat > /tmp/weibo_post.txt <<'EOF2'
微博正文
EOF2
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py post --text-file /tmp/weibo_post.txt --json
```

### 评论

```bash
cat > /tmp/weibo_comment.txt <<'EOF2'
评论正文
EOF2
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py comment \
  --post-url "https://weibo.com/用户UID/微博BID" \
  --text-file /tmp/weibo_comment.txt \
  --json
```

### 回复评论

桌面网页版里，单独一个裸 `comment_id` 通常不足以导航，所以建议提供 `--post-url` 和一段 `--comment-text` 用来定位评论：

```bash
cat > /tmp/weibo_reply.txt <<'EOF2'
回复正文
EOF2
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py reply-comment \
  --post-url "https://weibo.com/用户UID/微博BID" \
  --comment-id "评论ID或占位ID" \
  --comment-text "对方评论中的一段文字" \
  --text-file /tmp/weibo_reply.txt \
  --json
```

### 查看浏览量/互动数据

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py stats --mine --limit 20 --json
```

或单条：

```bash
python3 ~/nova_workspace/scripts/weibo/weibo_tool.py stats --post-url "https://weibo.com/用户UID/微博BID" --json
```

如果桌面页面没有显示浏览量，工具会返回 `views: null`，nova 不能猜。

---

## Debug

任何命令都可以加：

```bash
--debug
```

失败时会把截图、HTML、JSON 存到：

```bash
~/nova_workspace/state/weibo/debug/
```

如果微博改版导致选择器坏了，让 nova 把返回 JSON 里的 `debug_artifacts` 路径告诉你，再拿这些文件修脚本。

---

## 边界

工具不会绕过验证码、不会绕过账号风控、不会刷量、不会伪装成人类规避平台限制。出现登录、验证码、短信、安全检查时，工具返回 `blocked`，需要你手动处理。

当前第一版优先支持纯文本发微博、评论、回复、转发、点赞、读取和统计。图片上传还没实现。
