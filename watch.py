#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""西农信工·通知公告 云端巡检（GitHub Actions 用，只用标准库）

每次运行：
  1. 抓取列表页，解析出全部条目
  2. 和 state.json 里记录的链接比对，找出新条目
  3. 逐条抓正文，把标题、正文、附件链接一起发出去
  4. 更新 state.json（工作流负责提交回仓库）

环境变量：
  SMTP_HOST   默认 smtp.qq.com
  SMTP_PORT   默认 465（SSL）
  SMTP_USER   发件邮箱
  SMTP_PASS   邮箱授权码
  MAIL_TO     收件邮箱，默认同 SMTP_USER
  PUSHPLUS_TOKEN  可选的第二通道
  TEST_PUSH   设为 true 只发一条测试消息

用法：
  python watch.py              正常巡检
  python watch.py --seed       只把当前所有条目记为已读
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

PAGE_URL = os.environ.get("PAGE_URL", "https://cie.nwsuaf.edu.cn/dtytz/tzgg/index.htm")
PAGE_BASE = os.environ.get("PAGE_BASE", "https://cie.nwsuaf.edu.cn/dtytz/tzgg/")
PAGE_NAME = os.environ.get("PAGE_NAME", "通知公告")

# 每封邮件里每条通知正文最多保留多少字，超了截断（完整内容有原文链接）
MAX_BODY = 1500
# 一封邮件最多几条
MAX_ITEMS = 8

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ITEM_RE = re.compile(
    r"<li[^>]*>\s*<span[^>]*>\s*\[(\d{4}-\d{2}-\d{2})\]\s*</span>\s*"
    r"<a\s+([^>]*)>\s*(.*?)\s*</a>",
    re.S | re.I,
)
HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"', re.I)
TITLE_ATTR_RE = re.compile(r'title\s*=\s*"([^"]*)"', re.I)
TAG_RE = re.compile(r"<[^>]+>")
DIV_TAG_RE = re.compile(r"<div\b|</div>", re.I)
ATTACH_RE = re.compile(r'<a\b([^>]*\bdesc="textLink"[^>]*)>(.*?)</a>', re.S | re.I)


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

def slice_article(page):
    """截出 <div id="articleDiv"> ... 对应的整段（含嵌套 div）。"""
    i = page.find('id="articleDiv"')
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
    for m in ATTACH_RE.finditer(frag):
        attrs, inner = m.group(1), m.group(2)
        hm = HREF_RE.search(attrs)
        if not hm:
            continue
        href = html.unescape(hm.group(1).strip())
        if not href:
            continue
        url = href if href.lower().startswith("http") else urllib.parse.urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        name = html.unescape(TAG_RE.sub("", inner)).strip() or url.rsplit("/", 1)[-1]
        files.append((name, url))
    return files


def images_to_links(frag, base):
    """把正文里的图片换成可点的链接（邮件里不显示图，至少别丢）。"""
    def rep(m):
        sm = re.search(r'src\s*=\s*"([^"]+)"', m.group(0), re.I)
        if not sm:
            return ""
        u = html.unescape(sm.group(1).strip())
        u = u if u.lower().startswith("http") else urllib.parse.urljoin(base, u)
        return "\n[图片] %s\n" % u
    return re.sub(r"<img\b[^>]*>", rep, frag, flags=re.I)


def enrich(item):
    """抓正文，失败就留空（退回只发标题+链接）。"""
    try:
        page = http_get(item["url"])
    except Exception:  # noqa: BLE001
        return item
    frag = slice_article(page)
    item["files"] = parse_attachments(frag, item["url"])
    # 附件已经在「附件」段单独列出，正文里去掉，免得重复
    body_frag = ATTACH_RE.sub("", frag)
    body_frag = images_to_links(body_frag, item["url"])
    item["body"] = html_to_text(body_frag)
    return item


# ---------------------------------------------------------------- 组稿

def build_text(items):
    lines = ["【西农信工·%s】%d 条新通知" % (PAGE_NAME, len(items)), ""]
    for i, it in enumerate(items, 1):
        if len(items) > 1:
            lines.append("—— %d/%d ——" % (i, len(items)))
        lines.append("%s  %s" % (it["date"], it["title"]))
        lines.append("")

        body = it.get("body", "")
        if body:
            if len(body) > MAX_BODY:
                body = body[:MAX_BODY].rstrip() + "\n……（全文较长，剩余内容见原文）"
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
        if i < len(items):
            lines.append("")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_subject(items):
    if len(items) == 1:
        t = items[0]["title"]
        if len(t) > 40:
            t = t[:40] + "…"
        return "西农信工：" + t
    return "西农信工 %d 条新通知" % len(items)


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


# ---------------------------------------------------------------- 主流程

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"seen": {}, "last_run": None}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.write("\n")


def main():
    args = sys.argv[1:]
    seed = "--seed" in args
    dry = "--dry-run" in args

    if os.environ.get("TEST_PUSH", "").strip().lower() in ("1", "true", "yes"):
        text = (
            "【西农信工·通知公告】测试消息 ✅\n\n"
            "推送通道已经接通。\n"
            "以后学院官网发新通知，会以「标题 + 正文 + 附件 + 原文链接」的格式发到这里。"
        )
        ok = push_all(text, "西农信工：测试消息")
        print(text)
        print("测试推送:", "成功" if ok else "失败（没配任何通道？）")
        return 0 if ok else 1

    state = load_state()
    seen = state.setdefault("seen", {})

    try:
        page = http_get(PAGE_URL)
    except Exception as e:  # noqa: BLE001
        print("抓取失败: %r" % e, file=sys.stderr)
        return 1

    items = parse_items(page, PAGE_BASE)
    if not items:
        print("解析到 0 条，页面结构可能变了，本轮跳过", file=sys.stderr)
        return 1

    fresh = [it for it in items if it["url"] not in seen]
    print("共 %d 条，已读 %d 条，新增 %d 条" % (len(items), len(seen), len(fresh)))

    if seed or not seen:
        for it in items:
            seen[it["url"]] = it["date"]
        state["last_run"] = now_cn()
        if not dry:
            save_state(state)
        print("已初始化：记录 %d 条为已读" % len(items))
        return 0

    if not fresh:
        state["last_run"] = now_cn()
        if not dry:
            save_state(state)
        print("没有新通知")
        return 0

    if len(fresh) > 10:
        print("新增过多（%d 条），疑似改版，本轮不推送" % len(fresh))
        for it in fresh[:5]:
            print("  ", it["date"], it["title"])
        return 0

    for it in fresh:
        enrich(it)
        print("  抓到正文 %d 字，附件 %d 个：%s"
              % (len(it.get("body", "")), len(it.get("files") or []), it["title"]))

    text = build_text(fresh)
    subject = build_subject(fresh)
    print("---- 邮件预览 ----")
    print("主题：" + subject)
    print(text)
    print("-----------------")

    if dry:
        return 0

    if push_all(text, subject):
        for it in fresh:
            seen[it["url"]] = it["date"]
        state["last_run"] = now_cn()
        save_state(state)
        print("推送成功，已记录 %d 条" % len(fresh))
        return 0

    print("推送失败，state 未更新，下一轮会重试", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
