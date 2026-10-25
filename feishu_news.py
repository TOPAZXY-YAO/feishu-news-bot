# -*- coding: utf-8 -*-
"""飞书财经早报机器人：抓取财经 RSS -> 分类 -> 组装卡片 -> 推送到群自定义机器人 Webhook。

只用 Python 标准库，无第三方依赖。
用法:
    python feishu_news.py --dry-run          # 抓取并打印卡片预览，不发送
    python feishu_news.py                    # 正常推送（需 config.json 配好 webhook_url）
    python feishu_news.py --test             # 发送一张固定测试卡片，验证 webhook
    python feishu_news.py --config xx.json   # 指定配置文件，默认同目录 config.json
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parent
ATOM = "{http://www.w3.org/2005/Atom}"

DEFAULT_CONFIG = {
    "webhook_url": "",
    "secret": "",
    "hours_lookback": 24,          # 无历史状态时的默认回看窗口（小时）
    "max_per_category": 8,         # 每个板块最多条数
    "max_other": 4,                # 「其他要闻」最多条数
    "feeds": [
        {"name": "华尔街见闻", "url": "https://dedicated.wallstreetcn.com/rss.xml", "lang": "zh", "bias": ""},
        {"name": "WSJ", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "lang": "en", "bias": "global"}
    ]
}

CATEGORY_KEYS = {
    "A股与国内宏观": [
        "A股", "沪", "深市", "创业板", "科创板", "北交所", "央行动", "央行", "国务院", "发改委",
        "财政部", "证监会", "降准", "降息", "LPR", "MLF", "国债", "人民币", "CPI", "PPI", "GDP",
        "中国", "国内", "宏观", "政策", "楼市", "房地产", "促消费", "以旧换新", "两会", "增量政策",
        "专项债", "内需", "出口", "进口", "贸易", "中金", "中信", "A股收评", "股市收评", "IPO", "印花税",
    ],
    "全球市场与美股": [
        "美股", "纳指", "标普", "道指", "美联储", "Fed", "FOMC", "鲍威尔", "美债", "美元指数", "美元",
        "欧洲央行", "日央行", "日本央行", "日元", "黄金", "原油", "油价", "比特币", "加密", "OPEC",
        "特斯拉", "英伟达", "苹果", "微软", "谷歌", "Meta", "亚马逊", "AMD", "英特尔", "OpenAI",
        "欧股", "港股", "恒生", "恒指", "亚太股市", "新兴市场", "关税", "特朗普", "非农", "通胀",
        "高盛", "美银", "德银", "摩根", "对冲基金", "瑞银", "巴菲特", "华尔街",
    ],
    "行业与公司动态": [
        "半导体", "芯片", "光刻", "新能源", "光伏", "锂电", "储能", "汽车", "智驾", "固态电池",
        "AI", "人工智能", "大模型", "算力", "数据中心", "医药", "创新药", "白酒", "消费电子",
        "银行", "券商", "保险", "电商", "快递", "外卖", "手机", "财报", "业绩", "营收", "净利",
        "收购", "并购", "融资", "回购", "增持", "减持", "裁员", "上市", "发布会", "产能", "订单",
        "折叠屏", "iPhone", "出货", "涨价", "降价", "停产", "出海",
    ],
}

# 明显的营销/广告类标题关键词，直接过滤
PROMO_BLOCKLIST = ["见闻历", "直播预约", "活动报名", "会员专享", "限时优惠"]


# ---------- 基础工具 ----------

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    logdir = BASE / "logs"
    logdir.mkdir(exist_ok=True)
    with open(logdir / "run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # 环境变量优先于配置文件（GitHub Actions 用 Secrets 注入，避免密钥入库）
    if os.environ.get("FEISHU_WEBHOOK_URL"):
        cfg["webhook_url"] = os.environ["FEISHU_WEBHOOK_URL"]
    if os.environ.get("FEISHU_SECRET"):
        cfg["secret"] = os.environ["FEISHU_SECRET"]
    return cfg


def http_get(url: str, timeout: int = 15, retries: int = 2) -> bytes:
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"GET {url} 失败: {last_err}")


# ---------- 抓取与解析 ----------

def parse_feed(data: bytes) -> list:
    """返回 [{title, link, dt}]，dt 为 aware datetime 或 None。"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", data.decode("utf-8", errors="replace"))
    root = ET.fromstring(text.encode("utf-8"))
    items = []
    for it in root.findall(".//item") or root.findall(f".//{ATOM}entry"):
        title = (it.findtext("title") or it.findtext(f"{ATOM}title") or "").strip()
        link = it.findtext("link") or ""
        if not link:
            le = it.find(f"{ATOM}link")
            link = le.get("href") if le is not None else ""
        date_raw = it.findtext("pubDate") or it.findtext(f"{ATOM}updated") or it.findtext(f"{ATOM}published") or ""
        dt = None
        if date_raw:
            try:
                dt = parsedate_to_datetime(date_raw.strip())
            except Exception:
                try:
                    dt = datetime.fromisoformat(date_raw.strip().replace("Z", "+00:00"))
                except Exception:
                    dt = None
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))  # 中文源默认北京时间
        if title and link:
            items.append({"title": re.sub(r"\s+", " ", title), "link": link.strip(), "dt": dt})
    return items


def fetch_all(feeds: list) -> list:
    all_items = []
    for feed in feeds:
        try:
            items = parse_feed(http_get(feed["url"]))
            for it in items:
                it["source"] = feed["name"]
                it["bias"] = feed.get("bias", "")
            all_items.extend(items)
            log(f"源 {feed['name']}: 获取 {len(items)} 条")
        except Exception as e:
            log(f"源 {feed['name']} 失败（跳过）: {e}")
    return all_items


# ---------- 分类 ----------

def classify(item: dict) -> str:
    if item.get("bias"):
        return {"global": "全球市场与美股"}.get(item["bias"], "")
    title = item["title"]
    scores = {}
    for cat, keys in CATEGORY_KEYS.items():
        score = sum(1 for k in keys if k in title)
        if score:
            scores[cat] = score
    if not scores:
        return ""
    return max(scores, key=lambda c: (scores[c], -list(CATEGORY_KEYS).index(c)))


# ---------- 状态（去重 + 回看窗口） ----------

def load_state() -> dict:
    p = BASE / "state.json"
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen": [], "last_push": None}


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-500:]
    with open(BASE / "state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------- 卡片 ----------

WEEKDAYS = "一二三四五六日"


def build_card(items: list, cfg: dict, now: datetime) -> dict:
    state = load_state()
    seen = set(state["seen"])
    since = now - timedelta(hours=cfg["hours_lookback"])
    if state.get("last_push"):
        try:
            since = min(since, datetime.fromisoformat(state["last_push"]) - timedelta(hours=2))
        except Exception:
            pass

    fresh, kept_links = [], set()
    for it in items:
        link = it["link"]
        if link in seen or link in kept_links:
            continue
        if any(w in it["title"] for w in PROMO_BLOCKLIST):
            continue
        if it["dt"] and it["dt"] > now + timedelta(hours=1):
            continue  # 时钟偏差保护
        if it["dt"] and it["dt"] < since:
            continue
        kept_links.add(link)
        fresh.append(it)

    buckets = {c: [] for c in CATEGORY_KEYS}
    other = []
    for it in fresh:
        cat = classify(it)
        if cat and len(buckets[cat]) < cfg["max_per_category"]:
            buckets[cat].append(it)
        elif not cat and len(other) < cfg["max_other"]:
            other.append(it)

    for c in buckets:
        buckets[c].sort(key=lambda x: x["dt"] or now, reverse=True)
    other.sort(key=lambda x: x["dt"] or now, reverse=True)

    date_str = f"{now.year}年{now.month}月{now.day}日 周{WEEKDAYS[now.weekday()]}"
    greeting = "早安财经日报" if now.hour < 11 else ("午间财经速递" if now.hour < 17 else "晚间财经要闻")
    icons = {"A股与国内宏观": "📈", "全球市场与美股": "🌍", "行业与公司动态": "🏢"}
    elements = []
    total = sum(len(v) for v in buckets.values()) + len(other)
    if total == 0:
        elements.append({"tag": "markdown", "content": "今日暂无符合条件的新闻（数据源可能无更新）。"})

    for cat, its in buckets.items():
        if not its:
            continue
        lines = [f"**{icons[cat]} {cat}**"]
        for it in its:
            src = f"（{it['source']}）" if it["source"] == "WSJ" else ""
            lines.append(f"- [{it['title']}]({it['link']}){src}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        elements.append({"tag": "hr"})
    if other:
        lines = ["**📌 其他要闻**"]
        for it in other:
            lines.append(f"- [{it['title']}]({it['link']})")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        elements.append({"tag": "hr"})
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"共 {total} 条 ｜ 来源：华尔街见闻 · WSJ ｜ 本地机器人每日 08:00 自动推送"}],
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"☕ {greeting} · {date_str}"},
        },
        "elements": elements,
    }


def build_test_card() -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "✅ 飞书财经早报机器人 · 连通测试"},
        },
        "elements": [
            {"tag": "markdown", "content": "**Webhook 配置成功！**\n\n从明天起，每天早上 8:00 会在这里收到自动推送的财经早报。"},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "收到这条消息说明机器人已就绪"}]},
        ],
    }


# ---------- 发送 ----------

def sign(secret: str, timestamp: int) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def send(webhook_url: str, payload: dict, secret: str = "", retries: int = 3) -> dict:
    body = dict(payload)
    if secret:
        ts = int(time.time())
        body["timestamp"] = str(ts)
        body["sign"] = sign(secret, ts)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                webhook_url, data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            code = result.get("code", result.get("StatusCode"))
            if code == 0:
                return result
            raise RuntimeError(f"飞书返回错误: {result}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            last_err = RuntimeError(f"HTTP {e.code}: {detail}")
        except Exception as e:
            last_err = e
        log(f"发送失败（第 {attempt + 1} 次）: {last_err}，{5 * (attempt + 1)} 秒后重试")
        time.sleep(5 * (attempt + 1))
    raise last_err


# ---------- 主流程 ----------

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE / "config.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    now = datetime.now(timezone(timedelta(hours=8)))  # 北京时间

    if args.dry_run:
        items = fetch_all(cfg["feeds"])
        card = build_card(items, cfg, now)
        print(json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False, indent=2))
        return

    if not cfg.get("webhook_url"):
        log("config.json 中尚未配置 webhook_url，退出。")
        sys.exit(1)

    if args.test:
        send(cfg["webhook_url"], {"msg_type": "interactive", "card": build_test_card()}, cfg.get("secret", ""))
        log("测试卡片已发送。")
        return

    items = fetch_all(cfg["feeds"])
    if not items:
        log("所有数据源均失败，本次不推送、不计入去重状态。")
        sys.exit(2)

    state = load_state()
    card = build_card(items, cfg, now)
    total_note = card["elements"][-1]["elements"][0]["content"]
    n = int(re.search(r"共 (\d+) 条", total_note).group(1)) if re.search(r"共 (\d+) 条", total_note) else 0
    if n == 0:
        log("过滤后无新闻可推，跳过发送（不更新去重状态）。")
        return

    send(cfg["webhook_url"], {"msg_type": "interactive", "card": card}, cfg.get("secret", ""))
    state["seen"].extend(it["link"] for it in items)
    state["last_push"] = now.isoformat()
    save_state(state)
    log(f"推送成功，共 {n} 条。")


if __name__ == "__main__":
    main()
