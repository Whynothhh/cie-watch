#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""西农多个站点 通知公告 云端巡检（GitHub Actions 用，只用标准库）

监控三个栏目：
  信工学院  https://cie.nwsuaf.edu.cn/dtytz/tzgg/index.htm
  水建学院  https://sjxy.nwsuaf.edu.cn/tzggB/index.htm
  教务处    https://jiaowu.nwsuaf.edu.cn/tzggB/index.htm

每次运行：
  1. 逐页抓列表，和 state.json 里记录比对，找出新条目
  2. 逐条抓正文，把标题、正文、附件链接一起发出去
  3. 更新 state.json（工作流负责提交回仓库）

环境变量：
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / MAIL_TO
  PUSHPLUS_TOKEN  可选的第二通道
  TEST_PUSH       设为 true 只发一条测试消息

用法：
  python watch.py              正常巡检
  python watch.py --seed       把所有页面当前条目记为已读，不推送
  python watch.py --dry-run    抓取并打印，不推送、不写 state
"""

import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"

# 每个站点的正文容器标记各不相同，marker 是正文 div 的特征串
PAGES = [
    {
        "name": "信工",
        "url": "https://cie.nwsuaf.edu.cn/dtytz/tzgg/index.htm",
        "base": "https://cie.nwsuaf.edu.cn/dtytz/tzgg/",
        "marker": 'id="articleDiv"',
    },
    {
        "name": "水建",
        "url": "https://sjxy.nwsuaf.edu.cn/tzggB/index.htm",
        "base": "https://sjxy.nwsuaf.edu.cn/tzggB/",
        "marker": 'class="cont"',
    },
    {
        "name": "教务",
        "url": "https://jiaowu.nwsuaf.edu.cn/tzggB/index.htm",
        "base": "https://jiaowu.nwsuaf.edu.cn/tzggB/",
        "marker": 'class="article2"',
    },
]

MAX_BODY = 1200      # 每条正文最多保留多少字
MAX_ITEMS = 6        # 一封邮件最多几条
MAX_NEW_PER_PAGE = 10  # 单页一轮新增超过这个数，视为改版，跳过

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ITEM_RE = re.compile(
    r"<li[^>]*>\s*<span[^>]*>\s*[\[(（]\s*(\d{4}-\d{2}-\d{2})\s*[\])）]\s*</span>\s*"
    r"<a\s+([^>]*)>\s*(.*?)\s*</a>",
    re.S | re.I,
)
HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"', re.I)
TITLE_ATTR_RE = re.compile(r'title\s*=\s*"([^"]*)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")
DIV_TAG_RE = re.compile(r"<div\b|</div>", re.I)
ATTACH_RE = re.compile(r'<a\b([^>]*\bdesc="textLink"[^>]*)>(.*?)</a>', re.S | re.I)
ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
FILE_EXT_RE = re.compile(r"\.(docx?|xlsx?|pptx?|pdf|zip|rar|7z|txt)(\?|$)", re.I)


def now_cn():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- 抓取

def http_get(url, timeout=30):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_items(text, base):
    items, seen = [], set()
    for m in ITEM_RE.finditer(text):
        date, attrs, inner = m.group(1), m.group(2), m.group(3)
        hm = HREF_RE.search(attrs)
        if not hm:
            continue
        href = html.unescape(hm.group(1).strip())
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        tm = TITLE_ATTR_RE.search(attrs)
        title = html.unescape(tm.group(1)).strip() if tm else ""
        if not title:
            title = html.unescape(TAG_RE.sub("", inner)).strip()
        if not title:
            continue
        url = href if href.lower().startswith("http") else urllib.parse.urljoin(base, href)
        url = url.split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        items.append({"date": date, "title": title, "url": url})
    return items


# ---------------------------------------------------------------- 正文抽取

def slice_container(page, marker):
    """截出含 marker 的那个 div 的整段（照顾嵌套 div）。"""
    i = page.find(marker)
    if i < 0:
        return ""
    start = page.rfind("<div", 0, i)
    if start < 0:
        return ""
    depth = 0
    for m in DIV_TAG_RE.finditer(page, start):
        if m.group(0).lower().startswith("<div"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return page[start:m.end()]
    return page[start:]


def images_to_links(frag, base):
    def rep(m):
        sm = re.search(r'src\s*=\s*"([^"]+)"', m.group(0), re.I)
        if not sm:
            return ""
        u = html.unescape(sm.group(1).strip())
        u = u if u.lower().startswith("http") else urllib.parse.urljoin(base, u)
        # 模板自带的图标/装饰图没有意义，丢掉
        low = u.lower()
        if "attach.gif" in low or "/pub/" in low or "/g_style/" in low:
            return ""
        return "\n[图片] %s\n" % u
    return re.sub(r"<img\b[^>]*>", rep, frag, flags=re.I)


def html_to_text(frag):
    if not frag:
        return ""
    frag = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", frag)
    frag = re.sub(r"(?i)<br\s*/?>", "\n", frag)
    frag = re.sub(r"(?i)</p\s*>", "\n", frag)
    frag = re.sub(r"(?i)</tr\s*>", "\n", frag)
    frag = re.sub(r"(?i)</t[dh]\s*>", "  ", frag)
    frag = re.sub(r"(?i)</(div|li|h[1-6])\s*>", "\n", frag)
    frag = re.sub(r"<[^>]+>", "", frag)
    frag = html.unescape(frag)

    out, blank = [], False
    for line in frag.splitlines():
        s = line.replace("\u3000", " ").strip()
        if s:
            out.append(s)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def parse_attachments(frag, base):
    files, seen = [], set()

    def add(href, name):
        if not href:
            return
        url = href if href.lower().startswith("http") else urllib.parse.urljoin(base, href)
        url = url.split("#")[0]
        if url in seen:
            return
        seen.add(url)
        files.append((name or url.rsplit("/", 1)[-1], url))

    # 1) 教育 CMS 的标准附件锚点
    for m in ATTACH_RE.finditer(frag):
        hm = HREF_RE.search(m.group(1))
        if hm:
            add(html.unescape(hm.group(1).strip()),
                html.unescape(TAG_RE.sub("", m.group(2))).strip())

    # 2) 指向文档的普通链接（有些站点不用标准锚点）
    for m in ANCHOR_RE.finditer(frag):
        hm = HREF_RE.search(m.group(1))
        if not hm:
            continue
        href = html.unescape(hm.group(1).strip())
        if not FILE_EXT_RE.search(href):
            continue
        add(href, html.unescape(TAG_RE.sub("", m.group(2))).strip())

    return files


def enrich(item, marker):
    try:
        page = http_get(item["url"])
    except Exception:  # noqa: BLE001
        return item

    frag = slice_container(page, marker)
    if not frag:
        return item

    # 有的模板把标题和"作者/来源/发布日期"也放在同一个容器里
    frag = re.sub(r'(?is)<h[1-3][^>]*class="t"[^>]*>.*?</h[1-3]>', "", frag)
    frag = re.sub(r'(?is)<div[^>]*class="info"[^>]*>.*?</div>', "", frag)

    item["files"] = parse_attachments(frag, item["url"])
    body_frag = ATTACH_RE.sub("", frag)
    body_frag = images_to_links(body_frag, item["url"])
    item["body"] = html_to_text(body_frag)
    return item


# ---------------------------------------------------------------- 组稿

def build_text(items):
    n = len(items)
    names = []
    for it in items:
        if it["page"] not in names:
            names.append(it["page"])
    if len(names) == 1:
        head = "【西农%s·通知公告】%d 条新通知" % (names[0], n)
    else:
        head = "【西农】%d 条新通知（%s）" % (n, "、".join(names))

    lines = [head, ""]
    for i, it in enumerate(items, 1):
        if n > 1:
            lines.append("—— %d/%d ——" % (i, n))
        lines.append("【%s】%s  %s" % (it["page"], it["date"], it["title"]))
        lines.append("")

        body = it.get("body", "")
        if body:
            if len(body) > MAX_BODY:
                body = body[:MAX_BODY].rstrip() + "\n……（全文较长，剩余见原文）"
            lines.extend(body.splitlines())
            lines.append("")

        files = it.get("files") or []
        if files:
            lines.append("附件：")
            for name, u in files:
                lines.append("· " + name)
                lines.append("  " + u)
            lines.append("")

        lines.append("原文：" + it["url"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_subject(items):
    if len(items) == 1:
        t = items[0]["title"]
        if len(t) > 42:
            t = t[:42] + "…"
        return "西农：%s" % t
    names = []
    for it in items:
        if it["page"] not in names:
            names.append(it["page"])
    return "西农 %d 条新通知（%s）" % (len(items), "·".join(names))


# ---------------------------------------------------------------- 推送

def push_email(text, subject):
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    if not user or not password:
        return None
    host = os.environ.get("SMTP_HOST", "smtp.qq.com").strip()
    port = int(os.environ.get("SMTP_PORT", "465"))
    to = os.environ.get("MAIL_TO", user).strip() or user
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, password)
        s.sendmail(user, [to], msg.as_string())
    print("email: ok ->", to)
    return True


def push_pushplus(text, subject):
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        return None
    payload = {
        "token": token,
        "title": subject,
        "content": text.replace("\n", "<br>"),
        "template": "html",
    }
    req = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ok = body.get("code") == 200
    print("pushplus:", "ok" if ok else body)
    return ok


def push_all(text, subject):
    results = []
    for name, fn in (("email", lambda: push_email(text, subject)),
                     ("pushplus", lambda: push_pushplus(text, subject))):
        try:
            r = fn()
            if r is not None:
                results.append(r)
        except Exception as e:  # noqa: BLE001
            print("%s 失败: %r" % (name, e), file=sys.stderr)
            results.append(False)
    return any(results)


# ---------------------------------------------------------------- 状态

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8-sig") as f:
            s = json.load(f)
    except FileNotFoundError:
        return {"pages": {}, "last_run": None}

    # 旧版单页格式迁移到多页格式
    if "seen" in s and "pages" not in s:
        s = {"pages": {PAGES[0]["name"]: {"initialized": True, "seen": s["seen"]}},
             "last_run": s.get("last_run")}
    s.setdefault("pages", {})
    return s


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")


# ---------------------------------------------------------------- 主流程

def main():
    args = sys.argv[1:]
    seed = "--seed" in args
    dry = "--dry-run" in args

    if os.environ.get("TEST_PUSH", "").strip().lower() in ("1", "true", "yes"):
        text = (
            "【西农·通知公告】测试消息 ✅\n\n"
            "现在监控三个栏目：信工学院、水建学院、教务处。\n"
            "有新通知时，会以「标题 + 正文 + 附件 + 原文链接」的格式发到这里。"
        )
        ok = push_all(text, "西农：测试消息")
        print(text)
        print("测试推送:", "成功" if ok else "失败（没配任何通道？）")
        return 0 if ok else 1

    state = load_state()
    pages_state = state["pages"]
    fresh_all = []

    for page in PAGES:
        name = page["name"]
        ps = pages_state.setdefault(name, {"initialized": False, "seen": {}})
        try:
            text = http_get(page["url"])
        except Exception as e:  # noqa: BLE001
            print("[%s] 抓取失败: %r" % (name, e), file=sys.stderr)
            continue

        items = parse_items(text, page["base"])
        if not items:
            print("[%s] 解析到 0 条，跳过（页面结构可能变了）" % name, file=sys.stderr)
            continue

        seen = ps["seen"]
        fresh = [it for it in items if it["url"] not in seen]

        if seed or not ps["initialized"]:
            for it in items:
                seen[it["url"]] = it["date"]
            ps["initialized"] = True
            print("[%s] 初始化：%d 条记为已读" % (name, len(items)))
            continue

        if len(fresh) > MAX_NEW_PER_PAGE:
            print("[%s] 新增 %d 条，超过阈值，疑似改版，本轮跳过" % (name, len(fresh)))
            continue

        for it in fresh:
            it["page"] = name
            fresh_all.append(it)
        print("[%s] 共 %d 条，新增 %d 条" % (name, len(items), len(fresh)))

    state["last_run"] = now_cn()

    if not fresh_all:
        if not dry:
            save_state(state)
        print("没有新通知")
        return 0

    for it in fresh_all:
        page = next(p for p in PAGES if p["name"] == it["page"])
        enrich(it, page["marker"])
        print("  [%s] 正文 %d 字 / 附件 %d 个：%s"
              % (it["page"], len(it.get("body", "")), len(it.get("files") or []), it["title"]))

    batch = fresh_all[:MAX_ITEMS]
    text = build_text(batch)
    subject = build_subject(batch)
    print("---- 邮件预览 ----")
    print("主题：" + subject)
    print(text)
    print("-----------------")

    if dry:
        return 0

    if push_all(text, subject):
        for it in batch:
            pages_state[it["page"]]["seen"][it["url"]] = it["date"]
        save_state(state)
        print("推送成功，已记录 %d 条" % len(batch))
        return 0

    print("推送失败，state 未更新，下一轮会重试", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
