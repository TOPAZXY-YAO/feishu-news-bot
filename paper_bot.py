# -*- coding: utf-8 -*-
"""经管论文日报机器人：抓取 NBER / Elsevier 期刊 / AgEcon / arXiv -> 去重 -> 飞书卡片推送。

复用 feishu_news 的抓取、解析、签名、发送工具；去重状态存 paper_state.json
（与新闻机器人互不干扰）。Webhook 从环境变量 PAPER_WEBHOOK_URL / PAPER_SECRET 注入。

用法:
    python paper_bot.py --dry-run    # 抓取并打印卡片预览，不发送
    python paper_bot.py --test       # 发送测试卡片
    python paper_bot.py              # 正常推送
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feishu_news as news

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "paper_state.json"
WEEKDAYS = "一二三四五六日"

# section: nber / journal / agecon / arxiv；cap 为每次推送每板块上限
FEEDS = [
    {"name": "NBER 工作论文", "url": "https://www.nber.org/rss/new.xml", "section": "nber", "cap": 6},
    {"name": "发展经济学JDE", "url": "https://rss.sciencedirect.com/publication/science/03043878", "section": "journal", "cap": 3},
    {"name": "中国经济评论", "url": "https://rss.sciencedirect.com/publication/science/1043951X", "section": "journal", "cap": 3},
    {"name": "城市经济学JUE", "url": "https://rss.sciencedirect.com/publication/science/00941190", "section": "journal", "cap": 2},
    {"name": "World Development", "url": "https://rss.sciencedirect.com/publication/science/0305750X", "section": "journal", "cap": 2},
    {"name": "AgEcon 农经文库", "url": "https://ageconsearch.umn.edu/rss", "section": "agecon", "cap": 4},
    {"name": "arXiv econ.GN", "url": "http://export.arxiv.org/rss/econ.GN", "section": "arxiv", "cap": 2},
    {"name": "arXiv q-fin", "url": "http://export.arxiv.org/rss/q-fin", "section": "arxiv", "cap": 2},
]

SECTION_META = [
    ("nber", "NBER 工作论文", "📚"),
    ("journal", "期刊最新发表", "📖"),
    ("agecon", "AgEcon 农经文库", "🌾"),
    ("arxiv", "arXiv 预印本", "🧮"),
]


# ---------- 标题清理 ----------

def clean_title(t: str) -> str:
    t = re.sub(r"<[^>]+>", "", t)  # Elsevier 的 <em> 等内联标签
    return re.sub(r"\s+", " ", t).strip()


def split_nber(title: str):
    """NBER 标题形如 'Paper Title -- by Author A, Author B'，拆成 (标题, 作者)。"""
    m = re.match(r"(.+?)\s*--\s*by\s+(.+)$", title)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title, ""


# ---------- 去重状态（独立于新闻机器人） ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": [], "last_push": None}


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-800:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 卡片 ----------

def fetch_all_papers() -> list:
    all_items = []
    for feed in FEEDS:
        try:
            items = news.parse_feed(news.http_get(feed["url"]))
            for it in items:
                it["section"] = feed["section"]
                it["cap"] = feed["cap"]
                it["src"] = feed["name"]
            all_items.extend(items)
            news.log(f"源 {feed['name']}: 获取 {len(items)} 条")
        except Exception as e:
            news.log(f"源 {feed['name']} 失败（跳过）: {e}")
    return all_items


def build_paper_card(all_items: list, now: datetime) -> dict:
    state = load_state()
    seen = set(state["seen"])

    sections = {key: [] for key, _, _ in SECTION_META}
    feed_counts = {}  # 每个源各自的已选条数（同一板块可含多个源）
    kept_links = set()
    for it in all_items:
        link = it["link"]
        if link in seen or link in kept_links:
            continue
        title = clean_title(it["title"])
        if len(title) < 8:
            continue
        # 带日期的源（AgEcon）过滤一周前的旧条目，防止旧文献新上传刷屏
        if it.get("dt") and it["dt"] < now - timedelta(days=7):
            continue
        authors = ""
        if it["section"] == "nber":
            title, authors = split_nber(title)
        kept_links.add(link)
        sec = it["section"]
        if feed_counts.get(it["src"], 0) < it["cap"]:
            feed_counts[it["src"]] = feed_counts.get(it["src"], 0) + 1
            sections[sec].append((title, link, authors))

    date_str = f"{now.year}年{now.month}月{now.day}日 周{WEEKDAYS[now.weekday()]}"
    elements, total = [], 0
    for key, name, icon in SECTION_META:
        items = sections[key]
        if not items:
            continue
        total += len(items)
        lines = [f"**{icon} {name}**"]
        for title, link, authors in items:
            suffix = f"（{authors}）" if authors else ""
            lines.append(f"- [{title}]({link}){suffix}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        elements.append({"tag": "hr"})

    if not elements:
        elements = [{"tag": "markdown", "content": "今日各源均无新论文。"}]
    elif elements[-1].get("tag") == "hr":
        elements.pop()

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": f"共 {total} 篇 ｜ 来源：NBER · Elsevier · AgEcon · arXiv ｜ 每日 08:30 自动推送"}],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"📐 经管论文日报 · {date_str}"},
        },
        "elements": elements,
    }, total


def build_test_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "✅ 经管论文日报机器人 · 连通测试"},
        },
        "elements": [
            {"tag": "markdown", "content": "**Webhook 配置成功！**\n\n从明天起，每天早上 8:30 会在这里收到经管论文日报（NBER 工作论文、英文顶刊、农经文库、arXiv）。"},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "收到这条消息说明论文机器人已就绪"}]},
        ],
    }


# ---------- 主流程 ----------

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    import os
    webhook = os.environ.get("PAPER_WEBHOOK_URL", "")
    secret = os.environ.get("PAPER_SECRET", "")
    now = datetime.now(timezone(timedelta(hours=8)))

    if args.dry_run:
        items = fetch_all_papers()
        card, total = build_paper_card(items, now)
        print(json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False, indent=2))
        return

    if not webhook:
        news.log("未配置 PAPER_WEBHOOK_URL，退出。")
        sys.exit(1)

    if args.test:
        news.send(webhook, {"msg_type": "interactive", "card": build_test_card()}, secret)
        news.log("测试卡片已发送。")
        return

    items = fetch_all_papers()
    if not items:
        news.log("所有数据源均失败，本次不推送。")
        sys.exit(2)

    state = load_state()
    card, total = build_paper_card(items, now)
    if total == 0:
        news.log("无新论文，跳过发送（不更新去重状态）。")
        return

    news.send(webhook, {"msg_type": "interactive", "card": card}, secret)
    state["seen"].extend(it["link"] for it in items)
    state["last_push"] = now.isoformat()
    save_state(state)
    news.log(f"推送成功，共 {total} 篇。")


if __name__ == "__main__":
    main()
