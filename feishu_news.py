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

# 全球主要指数（腾讯行情代码）：早报图表数据源
MARKET_INDICES = ["sh000001", "sz399001", "sz399006", "hkHSI", "usDJI", "usIXIC", "usINX"]


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
        desc = (it.findtext("description") or it.findtext(f"{ATOM}summary") or "").strip()
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
            items.append({"title": re.sub(r"\s+", " ", title), "link": link.strip(),
                          "dt": dt, "desc": desc})
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


def filter_fresh(items: list, cfg: dict, now: datetime, state: dict = None) -> list:
    """按去重状态与时间窗过滤。state=None 时不过滤（--preview 用）。"""
    if state is None:
        state = {"seen": [], "last_push": None}
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
    return fresh


# ---------- 行情图表与 AI 分析 ----------

def fetch_quotes() -> list:
    """腾讯行情接口取全球主要指数，返回 [{name, price, prev, pct}]；失败抛异常。"""
    url = "https://qt.gtimg.cn/q=" + ",".join(MARKET_INDICES)
    text = http_get(url).decode("gbk", errors="replace")
    quotes = []
    for line in text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        f = line.split("=", 1)[1].strip('"').split("~")
        if len(f) > 32 and f[1] and f[3]:
            try:
                quotes.append({"name": f[1], "price": float(f[3]),
                               "prev": float(f[4]), "pct": float(f[32])})
            except ValueError:
                continue
    if not quotes:
        raise RuntimeError("行情接口未返回有效数据")
    return quotes


def make_chart(quotes: list, now: datetime) -> str:
    """涨跌幅横向条形图（中式配色：涨红跌绿），返回 PNG 路径。"""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt

    for cand in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei"):
        if any(f.name == cand for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False

    qs = sorted(quotes, key=lambda q: q["pct"])
    names = [q["name"] for q in qs]
    pcts = [q["pct"] for q in qs]
    colors = ["#d93a3a" if p >= 0 else "#1a9e5c" for p in pcts]

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    bars = ax.barh(names, pcts, color=colors, height=0.62)
    for bar, pct in zip(bars, pcts):
        off = 0.03 if pct >= 0 else -0.03
        ax.text(pct + off, bar.get_y() + bar.get_height() / 2,
                f"{pct:+.2f}%", va="center",
                ha="left" if pct >= 0 else "right", fontsize=10,
                color="#333333")
    ax.axvline(0, color="#888888", lw=0.8)
    ax.set_title(f"全球主要指数涨跌幅 %（{now.strftime('%m-%d %H:%M')} 北京时间）",
                 fontsize=13, pad=10)
    ax.spines[["top", "right"]].set_visible(False)
    ax.margins(y=0.02)
    xmin, xmax = ax.get_xlim()
    ax.set_xlim(xmin - 0.6, xmax + 0.6)
    fig.tight_layout()
    path = str(BASE / "market_chart.png")
    fig.savefig(path)
    plt.close(fig)
    return path


def upload_image(png_path: str) -> str:
    """用小财应用凭证上传图片，返回 image_key（需应用有 im:resource 权限）。"""
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise RuntimeError("缺少 FEISHU_APP_ID/FEISHU_APP_SECRET，无法上传图片")
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = json.loads(resp.read())["tenant_access_token"]

    import uuid
    boundary = uuid.uuid4().hex
    with open(png_path, "rb") as f:
        img_bytes = f.read()
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="chart.png"\r\n'
            f"Content-Type: image/png\r\n\r\n").encode() + img_bytes + \
        f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        raise RuntimeError(f"图片上传失败: {result.get('msg')}")
    return result["data"]["image_key"]


def zhipu_analysis(headlines: list, quotes: list) -> str:
    """智谱免费模型生成今日解读（3-4 条要点 + 风险提示），失败返回空串。"""
    key = os.environ.get("ZHIPU_API_KEY", "")
    if not key:
        return ""
    now = datetime.now(timezone(timedelta(hours=8)))
    idx_line = " ".join(f"{q['name']}{q['pct']:+.2f}%" for q in quotes)
    prompt = (
        "你是财经早报编辑。基于下面的隔夜与近期要闻标题、以及全球主要指数表现，"
        "写一份简短的「今日解读」：3-4 条要点（每条不超过40字，指出市场主线、"
        "逻辑或值得关注的信号）+ 最后单独一条风险提示（不超过40字）。\n"
        "要求：中文；直接输出条目，每条一行以 - 开头；不要客套话和总结；"
        "不编造标题之外的具体数字。\n\n"
        f"日期：{now.strftime('%Y-%m-%d %A')}\n"
        f"指数表现：{idx_line}\n"
        "要闻标题：\n" + "\n".join(f"- {t}" for t in headlines[:12])
    )
    last_err = None
    for model in ["glm-4-flash-250414", "glm-4.5-flash", "glm-4.7-flash"]:
        for attempt in range(2):
            try:
                body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.4, "max_tokens": 600,
                        "thinking": {"type": "disabled"}}
                req = urllib.request.Request(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                text = (data["choices"][0]["message"].get("content") or "").strip()
                if text:
                    return text
                break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code == 401:
                    return ""
                if e.code == 429 and attempt < 1:
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                last_err = e
                break
    log(f"AI 分析失败（降级跳过）: {last_err}")
    return ""


def enrich(fresh: list, cfg: dict, now: datetime) -> dict:
    """图表 + AI 分析编排，任何一步失败都降级（extras 对应项为 None）。"""
    extras = {"img_key": None, "analysis": None, "quotes": None}
    try:
        quotes = fetch_quotes()
        extras["quotes"] = quotes
    except Exception as e:
        log(f"行情获取失败（图表降级）: {e}")
        quotes = []
    if quotes:
        try:
            png = make_chart(quotes, now)
            extras["img_key"] = upload_image(png)
            log("行情图表已生成并上传")
        except Exception as e:
            log(f"图表生成/上传失败（降级跳过）: {e}")
    if fresh:
        titles = [it["title"] for it in fresh]
        try:
            extras["analysis"] = zhipu_analysis(titles, quotes)
            if extras["analysis"]:
                log("AI 今日解读已生成")
        except Exception as e:
            log(f"AI 分析异常（降级跳过）: {e}")
    return extras

def build_card(fresh: list, cfg: dict, now: datetime, extras: dict = None) -> dict:
    extras = extras or {}
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
    if extras.get("img_key"):
        elements.append({"tag": "img", "img_key": extras["img_key"],
                         "alt": {"tag": "plain_text", "content": "全球主要指数涨跌幅图表"}})
    if extras.get("analysis"):
        elements.append({"tag": "markdown",
                         "content": f"**🤖 AI 今日解读**\n{extras['analysis']}"})
        elements.append({"tag": "hr"})
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
        "elements": [{"tag": "plain_text",
                      "content": f"共 {total} 条 ｜ 来源：华尔街见闻 · WSJ ｜ 本地机器人每日 08:00 自动推送 ｜ AI 解读由 GLM 生成，仅供参考，不构成投资建议"}],
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


def send_all(cfg: dict, payload: dict) -> int:
    """向全部 webhook 推送（webhook_url 逗号分隔多个群），返回成功数。
    secret 同样按逗号与 webhook 一一对应，缺位视为无签名。"""
    webhooks = [w.strip() for w in (cfg.get("webhook_url") or "").split(",") if w.strip()]
    secrets = [s.strip() for s in (cfg.get("secret") or "").split(",")]
    ok = 0
    for i, wh in enumerate(webhooks):
        try:
            send(wh, payload, secrets[i] if i < len(secrets) else "")
            ok += 1
            log(f"推送成功 -> {wh[:66]}…")
        except Exception as e:
            log(f"推送失败 -> {wh[:66]}…: {e}")
    return ok


# ---------- 主流程 ----------

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE / "config.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--preview", action="store_true",
                    help="跳过去重与时间窗（演示新功能用），不更新去重状态")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    now = datetime.now(timezone(timedelta(hours=8)))  # 北京时间

    if args.dry_run:
        items = fetch_all(cfg["feeds"])
        state = load_state()
        fresh = filter_fresh(items, cfg, now, state)
        extras = enrich(fresh, cfg, now)
        card = build_card(fresh, cfg, now, extras)
        print(json.dumps({"msg_type": "interactive", "card": card}, ensure_ascii=False, indent=2))
        return

    if not cfg.get("webhook_url"):
        log("config.json 中尚未配置 webhook_url，退出。")
        sys.exit(1)

    if args.test:
        ok = send_all(cfg, {"msg_type": "interactive", "card": build_test_card()})
        if ok == 0:
            sys.exit(2)
        log(f"测试卡片已发送至 {ok} 个群。")
        return

    items = fetch_all(cfg["feeds"])
    if not items:
        log("所有数据源均失败，本次不推送、不计入去重状态。")
        sys.exit(2)

    state = load_state()
    fresh = filter_fresh(items, cfg, now, None if args.preview else state)
    if not fresh:
        log("过滤后无新闻可推，跳过发送（不更新去重状态）。")
        return
    extras = enrich(fresh, cfg, now)
    card = build_card(fresh, cfg, now, extras)
    total_note = card["elements"][-1]["elements"][0]["content"]
    n = int(re.search(r"共 (\d+) 条", total_note).group(1)) if re.search(r"共 (\d+) 条", total_note) else 0

    ok = send_all(cfg, {"msg_type": "interactive", "card": card})
    if args.preview:
        log(f"预览推送完成（{n} 条，{ok} 个群送达），不更新去重状态。")
        return
    if ok == 0:
        log("所有群均推送失败，不更新去重状态（下次重推）。")
        sys.exit(2)
    state["seen"].extend(it["link"] for it in items)
    state["last_push"] = now.isoformat()
    save_state(state)
    log(f"推送完成，共 {n} 条，送达 {ok} 个群。")


if __name__ == "__main__":
    main()
