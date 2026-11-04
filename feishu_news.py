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


def upload_image_bytes(img: bytes) -> str:
    """上传图片字节，返回 image_key（需应用有 im:resource 权限）。"""
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
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="img.png"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + img + \
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


def upload_image(png_path: str) -> str:
    with open(png_path, "rb") as f:
        return upload_image_bytes(f.read())


def extract_first_img(desc: str) -> str:
    """从 RSS description 的 HTML 里提取第一张图片 URL。"""
    m = re.search(r'<img[^>]+src="(https?://[^"]+)"', desc or "")
    return m.group(1) if m else ""


def _llm_request(provider: str, model: str, key: str, messages: list, max_tokens: int, temperature: float):
    """构造各厂商的 chat completions 请求（均为 OpenAI 兼容格式）。"""
    if provider == "deepseek":
        url = "https://api.deepseek.com/chat/completions"
        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens}
    else:  # zhipu
        url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        body = {"model": model, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens, "thinking": {"type": "disabled"}}
    return urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})


def llm_complete(messages: list, max_tokens: int = 600, temperature: float = 0.4) -> str:
    """非流式 LLM 调用：DeepSeek 主力，智谱免费模型兜底；全失败抛异常。"""
    deepseek = os.environ.get("DEEPSEEK_API_KEY", "")
    zhipu = os.environ.get("ZHIPU_API_KEY", "")
    attempts = []
    if deepseek:
        attempts.append(("deepseek", "deepseek-chat", deepseek))
    if zhipu:
        attempts += [("zhipu", m, zhipu) for m in
                     ("glm-4-flash-250414", "glm-4.5-flash", "glm-4.7-flash")]
    if not attempts:
        raise RuntimeError("未配置任何 LLM API Key")
    last_err = None
    for provider, model, key in attempts:
        for attempt in range(2):
            try:
                req = _llm_request(provider, model, key, messages, max_tokens, temperature)
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                text = (data["choices"][0]["message"].get("content") or "").strip()
                if text:
                    return text
                last_err = "空回复"
                break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code in (401, 402):  # key 无效/余额不足，换下一家
                    break
                if e.code == 429 and attempt < 1:
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                last_err = e
                break
    raise RuntimeError(f"所有模型均失败: {last_err}")


def ai_newspaper(headlines: list, quotes: list):
    """AI 主编：从要闻中筛选 6 条最重要新闻（各配一句摘要）+ 写今日解读。
    返回 (analysis: str, top: list[dict])；失败抛异常由调用方降级。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    idx_line = " ".join(f"{q['name']}{q['pct']:+.2f}%" for q in quotes)
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(headlines[:30]))
    prompt = (
        "你是财经早报主编。完成两项任务：\n"
        "任务一「今日解读」：基于要闻与指数表现，写 3-4 条要点（每条≤40字，指出市场主线/逻辑/信号）"
        "+ 最后一条风险提示（≤40字）。\n"
        "任务二「头条筛选」：从要闻中挑出 6 条最重要的，按重要性排序，各配一句不超过40字的中文摘要"
        "（英文新闻也用中文摘要），不编造标题之外的信息。\n"
        "只输出一个 JSON 对象，不要任何其他文字或代码块标记：\n"
        '{"analysis": ["- 要点1", "- 要点2", "- 要点3", "- ⚠️ 风险提示"], '
        '"top": [{"no": 标题编号数字, "summary": "一句摘要"}, ...共6条]}\n\n'
        f"日期：{now.strftime('%Y-%m-%d %A')}\n"
        f"指数表现：{idx_line}\n要闻标题：\n{numbered}"
    )
    text = llm_complete([{"role": "user", "content": prompt}], max_tokens=900, temperature=0.3)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"AI 未返回 JSON: {text[:120]}")
    data = json.loads(m.group(0))
    analysis = "\n".join(str(x) for x in data.get("analysis", []))
    top = []
    for p in data.get("top", [])[:6]:
        try:
            idx = int(p["no"]) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(headlines):
            top.append({"title": headlines[idx], "summary": str(p.get("summary", "")).strip()})
    if not top:
        raise RuntimeError("AI 未选出头条")
    return analysis, top


def attach_front_images(front: list, fresh: list, max_images: int = 3):
    """为前几条头条下载并上传配图（原图来自 RSS description）。"""
    by_title = {it["title"]: it for it in fresh}
    n_img = 0
    for t in front:
        if n_img >= max_images:
            break
        it = by_title.get(t["title"])
        img_url = extract_first_img(it.get("desc", "")) if it else ""
        if not img_url:
            continue
        try:
            t["img_key"] = upload_image_bytes(http_get(img_url, timeout=30))
            n_img += 1
        except Exception as e:
            log(f"头条配图失败（跳过）: {e}")


# ---------- 报纸图片版 ----------

def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def build_materials(fresh: list, max_items: int = 12, text_len: int = 420) -> list:
    """把 fresh 条目转成 AI 素材：编号 + 标题 + 正文截取 + 首图 URL。"""
    materials = []
    for i, it in enumerate(fresh[:max_items]):
        materials.append({
            "no": i + 1,
            "title": it["title"],
            "text": strip_html(it.get("desc", ""))[:text_len],
            "img_url": extract_first_img(it.get("desc", "")),
            "item": it,
        })
    return materials


def ai_newspaper_full(materials: list, quotes: list) -> dict:
    """AI 主编产出四页报纸全部内容：头条深度报道 + 要闻 + 三组简讯 + 今日解读 + 深度综述。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    idx_line = "；".join(f"{q['name']}{q['pct']:+.2f}%" for q in quotes)
    mat = "\n\n".join(
        f"【{m['no']}】{m['title']}\n{m['text'] or '（无正文，仅标题）'}" for m in materials)
    prompt = (
        "你是资深财经报纸主编，为一份四页「报纸图片版」组稿。基于素材正文撰写，读者看图即可完全了解新闻，"
        "绝不点链接。所有摘要信息完整（发生了什么+关键数字+为何重要+后续关注什么），数字只来自素材，不编造。\n"
        "1. lead：选今日最重要新闻，写 150-220 字深度报道（背景+事实+影响面）。\n"
        "2. top：再选 4 条重要新闻，各写 100-140 字深度摘要。\n"
        "3. market_briefs：全球市场相关 4 条，各 60-90 字。\n"
        "4. cn_briefs：A股与国内宏观相关 4 条，各 60-90 字。\n"
        "5. industry_briefs：行业与公司相关 4 条，各 60-90 字。\n"
        "6. comment：今日解读 5 条要点（每条≤40字）+ 最后一条风险提示（≤40字），每条以 - 开头。\n"
        "7. review：600-900 字深度市场综述，分三段（用两个空行分隔）：第一段串联当日主线与"
        "资金/情绪逻辑；第二段给出对后续 1-2 周的情景推演（各情景的触发条件与对应板块影响）；"
        "第三段列出 3-4 个具体观察点（数据、事件、价位）。观点鲜明、有逻辑链，不确定处注明。\n"
        "8. calendar：基于素材与常规日程，列出未来 5 个值得关注的财经事件/数据（非农、CPI、"
        "财报、央行动态、行业事件等），when 用简短时间表述（如 周四晚 / 本周内 / 11月中旬），"
        "event ≤30字。素材未提及的不要编造具体日期，用模糊表述。\n"
        "英文素材全部用中文。只输出 JSON（无其他文字/代码块标记）：\n"
        '{"lead": {"no": 编号, "summary": "..."}, "top": [{"no": 编号, "summary": "..."}, ...4条], '
        '"market_briefs": [{"no": 编号, "brief": "..."}, ...4条], '
        '"cn_briefs": [{"no": 编号, "brief": "..."}, ...4条], '
        '"industry_briefs": [{"no": 编号, "brief": "..."}, ...4条], '
        '"comment": ["- ...", ...], "calendar": [{"when": "...", "event": "..."}, ...5条], '
        '"review": "…"}\n\n'
        f"日期：{now.strftime('%Y-%m-%d %A')}\n指数表现：{idx_line}\n\n素材：\n{mat}"
    )
    text = llm_complete([{"role": "user", "content": prompt}], max_tokens=4400, temperature=0.35)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"AI 未返回 JSON: {text[:120]}")
    data = json.loads(m.group(0))
    by_no = {m["no"]: m for m in materials}

    def pick(entry, field):
        no = int(entry.get("no", 0))
        return by_no.get(no), str(entry.get(field, "")).strip()

    lead_no, lead_sum = pick(data.get("lead") or {}, "summary")
    if not lead_no:
        raise RuntimeError("AI 未选出头条")

    def group(key, field):
        out = []
        for e in data.get(key, []):
            item, s = pick(e, field)
            if item and s:
                out.append({"item": item, field: s})
        return out

    review = str(data.get("review", "")).strip()
    return {"lead": {"item": lead_no, "summary": lead_sum},
            "top": group("top", "summary"),
            "market_briefs": group("market_briefs", "brief"),
            "cn_briefs": group("cn_briefs", "brief"),
            "industry_briefs": group("industry_briefs", "brief"),
            "comment": [str(x) for x in data.get("comment", [])],
            "calendar": [{"when": str(e.get("when", "")), "event": str(e.get("event", ""))}
                         for e in data.get("calendar", [])[:5] if e.get("event")],
            "review": review}


def _wrap(text: str, width_units: float) -> list:
    """中文按 1 个单位、ASCII 按 0.62 个单位贪心换行。"""
    lines, cur, w = [], [], 0.0
    for ch in text:
        cw = 1.0 if ord(ch) > 0x2E80 else 0.62
        if w + cw > width_units and cur:
            lines.append("".join(cur))
            cur, w = [], 0.0
            if ch == " ":
                continue
        cur.append(ch)
        w += cw
    if cur:
        lines.append("".join(cur))
    return lines


def render_newspaper(materials: list, layout: dict, quotes: list, now: datetime,
                     lead_img_url: str = "") -> str:
    """渲染整版报纸 PNG，返回文件路径。版面用动态游标布局（行高与宽度按字号精确计算）。"""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    for cand in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei"):
        if any(f.name == cand for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False

    BG, INK, SUB, ACC = "#fbf9f1", "#1a1a1a", "#555555", "#8c1f1f"
    UP, DOWN = "#c93030", "#1a9e5c"
    FIG_W, FIG_H = 10.0, 12.3
    issue_no = (now.date() - datetime(2026, 10, 25).date()).days + 1

    fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    def chars(w_frac, fs):
        """宽 w_frac 的区域内，字号 fs 每行可容纳的全角字符数。"""
        return w_frac * FIG_W * 72 / fs

    def lh(fs):
        """字号 fs 的一行高度（画布高度比例），1.45 倍行距。"""
        return fs * 1.45 / (72 * FIG_H)

    def rule(y, lw=1.6, color=INK, x0=0.05, x1=0.95):
        ax.plot([x0, x1], [y, y], color=color, lw=lw, solid_capstyle="butt")

    def text_block(x, y, text, w_frac, fs, **kw):
        """画自动换行文本块，返回新的 y。kw 含 color/weight 等。"""
        for ln in _wrap(text, chars(w_frac, fs)):
            y -= lh(fs)
            ax.text(x, y, ln, fontsize=fs, va="top", **kw)
        return y

    # ── 报头 ──
    ax.text(0.5, 0.972, "早安财经日报", ha="center", va="center",
            fontsize=34, fontweight="bold", color=INK)
    ax.text(0.05, 0.968, f"第 {issue_no} 期", ha="left", va="center", fontsize=11, color=SUB)
    ax.text(0.95, 0.968, now.strftime("%Y年%m月%d日 %A"), ha="right", va="center",
            fontsize=11, color=SUB)
    rule(0.9515, 2.2)
    rule(0.9475, 0.6)

    # ── 指数速览条 ──
    y0 = 0.924
    if quotes:
        n = len(quotes)
        for i, q in enumerate(quotes):
            x = 0.055 + i * (0.89 / n)
            color = UP if q["pct"] >= 0 else DOWN
            ax.text(x, y0 + 0.011, q["name"], fontsize=8.2, color=SUB, ha="left")
            ax.text(x, y0 - 0.009, f"{q['pct']:+.2f}%", fontsize=10.5,
                    color=color, ha="left", fontweight="bold")
    rule(0.899, 0.7, "#999999")

    # ── 头条（左文右图，动态游标）──
    lead = layout["lead"]
    it = lead["item"]
    ax.text(0.05, 0.884, "今日头条", fontsize=11, color=ACC, fontweight="bold", ha="left")
    ax.text(0.125, 0.884, it["title"][:38], fontsize=9, color="#999999", ha="left", va="center")

    y = 0.872
    y = text_block(0.05, y, it["title"], 0.55, 15.5, color=INK, fontweight="bold")
    y -= 0.006
    y = text_block(0.05, y, lead["summary"], 0.55, 10.8, color="#2a2a2a")
    if y > 0.705:
        y = 0.705  # 头条区下限
    lead_bottom = y
    if lead_img_url:
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(http_get(lead_img_url, timeout=30)))
            img_top, img_h = 0.872, max(0.10, min(0.17, 0.872 - lead_bottom - 0.01))
            ax_img = fig.add_axes([0.645, img_top - img_h, 0.305, img_h])
            ax_img.imshow(img, aspect="auto")
            ax_img.set_xticks([])
            ax_img.set_yticks([])
            for sp in ax_img.spines.values():
                sp.set_color("#cccccc")
        except Exception as e:
            log(f"头条配图加载失败（纯文字版式）: {e}")
    rule(max(lead_bottom - 0.012, 0.70), 1.0, "#bbbbbb")

    # ── 要闻（3 条：标题+摘要，动态游标）──
    y = min(lead_bottom - 0.03, 0.672)
    for t in layout["top"][:3]:
        item = t["item"]
        y = text_block(0.05, y, item["title"], 0.9, 12, color=INK, fontweight="bold")
        y -= 0.003
        y = text_block(0.05, y, t["summary"], 0.9, 9.8, color="#333333")
        y -= 0.010
        rule(y + 0.006, 0.5, "#dddddd")
        y -= 0.010

    # ── 三栏简讯 ──
    cols_top = y - 0.014
    col_w = 0.292
    briefs = layout["briefs"]

    def cat_of(item):
        fake = {"title": item["title"], "desc": item.get("text", ""), "dt": None,
                "source": item["item"]["source"]}
        return classify(fake)

    buckets = {0: [], 1: [], 2: []}
    order = {0: "A股与国内宏观", 1: "全球市场与美股", 2: "行业与公司动态"}
    rest = []
    for b in briefs:
        slot = next((k for k, v in order.items() if v == cat_of(b["item"])), None)
        if slot is not None and len(buckets[slot]) < 2:
            buckets[slot].append(b)
        else:
            rest.append(b)
    for k in buckets:
        while len(buckets[k]) < 2 and rest:
            buckets[k].append(rest.pop(0))

    cols_bottom = cols_top
    for k, title in enumerate(["A股·宏观", "全球市场", "行业·公司"]):
        x = 0.05 + k * (col_w + 0.031)
        ax.plot([x - 0.014, x - 0.014], [cols_top + 0.004, cols_top - 0.205],
                color="#dddddd", lw=0.8)
        ax.text(x, cols_top, title, fontsize=12.5, color=ACC, fontweight="bold", va="top")
        yy = cols_top - lh(12.5) - 0.004
        for b in buckets[k][:2]:
            yy = text_block(x, yy, "▪ " + b["item"]["title"], col_w, 9.6,
                            color=INK, fontweight="bold")
            yy = text_block(x, yy, b["brief"], col_w, 8.8, color="#333333")
            yy -= 0.008
        cols_bottom = min(cols_bottom, yy)

    # ── 今日解读框 ──
    box_top = cols_bottom - 0.016
    n_lines = sum(len(_wrap(c.lstrip("- "), chars(0.83, 9.6))) for c in layout["comment"][:5])
    box_h = 0.032 + n_lines * lh(9.6) + 0.012
    ax.add_patch(FancyBboxPatch((0.05, box_top - box_h), 0.9, box_h,
                                boxstyle="round,pad=0.006,rounding_size=0.010",
                                fc="#f2ede2", ec="#c9b98a", lw=1.0))
    ax.text(0.072, box_top - 0.014, "今日解读", fontsize=12.5,
            color=ACC, fontweight="bold", va="top")
    yy = box_top - 0.014 - lh(12.5)
    for c in layout["comment"][:5]:
        wrapped = _wrap(c.lstrip("- "), chars(0.83, 9.6))
        for j, ln in enumerate(wrapped[:2]):
            yy -= lh(9.6)
            ax.text(0.072, yy, ("▪ " if j == 0 else "   ") + ln,
                    fontsize=9.6, color="#2a2a2a", va="top")

    # ── 页脚 ──
    rule(box_top - box_h - 0.014, 0.6, "#999999")
    ax.text(0.05, box_top - box_h - 0.030,
            f"来源：华尔街见闻 · WSJ ｜ 主编：DeepSeek ｜ 第 {issue_no} 期",
            fontsize=8.5, color=SUB)
    ax.text(0.95, box_top - box_h - 0.030, "AI 生成内容仅供参考，不构成投资建议",
            fontsize=8.5, color=SUB, ha="right")

    path = str(BASE / "newspaper.png")
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def build_newspaper(fresh: list, cfg: dict, now: datetime):
    """完整报纸流水线：素材 → AI 组稿 → 渲染。成功返回 (paths, layout)，失败返回 (None, 原因)。
    paths[0]=主报（必得），paths[1]=期货版（独立降级，失败则只有主报）。"""
    try:
        materials = build_materials(fresh)
        if not materials:
            return None, "无素材"
        quotes = []
        try:
            quotes = fetch_quotes()
        except Exception as e:
            log(f"行情获取失败（报纸无指数条）: {e}")
        layout = ai_newspaper_full(materials, quotes)
        lead_img = layout["lead"]["item"].get("img_url", "")
        # 单页版式：头条 + 3 条要闻 + 三栏简讯（全球/宏观/行业各2条）+ 解读
        layout_single = {
            "lead": layout["lead"],
            "top": layout["top"][:3],
            "briefs": (layout.get("market_briefs", [])[:2]
                       + layout.get("cn_briefs", [])[:2]
                       + layout.get("industry_briefs", [])[:2]),
            "comment": layout["comment"],
        }
        main_path = render_newspaper(materials, layout_single, quotes, now, lead_img)
        paths = [main_path]
        headlines = [it["title"] for it in fresh]
        # 期货版（独立流水线，任一步失败只跳过该版）
        try:
            import newspaper_pages as npages
            fquotes = npages.fetch_futures_quotes()
            fhistory = npages.fetch_futures_history()
            comment = npages.ai_futures_comment(fquotes, headlines)
            fpath = npages.render_futures_page(now, fquotes, fhistory, comment, headlines)
            paths.append(fpath)
            log(f"期货版已生成（{len(fquotes)} 个品种）")
        except Exception as e:
            log(f"期货版生成失败（本次只有主报）: {e}")
        # 加密货币版（独立降级）
        try:
            import market_pages as mpages
            cquotes = mpages.fetch_crypto_quotes()
            btc = mpages.fetch_crypto_history("bitcoin")
            eth = mpages.fetch_crypto_history("ethereum")
            ccomment = mpages.ai_crypto_comment(cquotes, headlines)
            cpath = mpages.render_crypto_page(now, cquotes, btc, eth, ccomment)
            paths.append(cpath)
            log(f"加密货币版已生成（{len(cquotes)} 个币种）")
        except Exception as e:
            log(f"加密版生成失败（本次缺）: {e}")
        # 全球国债版（独立降级）
        try:
            yields = mpages.fetch_bond_yields()
            curve = mpages.fetch_fred_curve()
            bcomment = mpages.ai_bonds_comment(yields, headlines)
            bpath = mpages.render_bonds_page(now, yields, curve, bcomment)
            paths.append(bpath)
            log(f"全球国债版已生成（{len(yields)} 国）")
        except Exception as e:
            log(f"国债版生成失败（本次缺）: {e}")
        return paths, layout
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def enrich(fresh: list, cfg: dict, now: datetime) -> dict:
    """图表 + AI 主编编排，任何一步失败都降级（extras 对应项为 None）。"""
    extras = {"img_key": None, "analysis": None, "quotes": None, "front": None}
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
            analysis, front = ai_newspaper(titles, quotes)
            extras["analysis"] = analysis
            attach_front_images(front, fresh)
            extras["front"] = front
            log(f"AI 主编完成：头条 {len(front)} 条")
        except Exception as e:
            log(f"AI 主编失败（降级全量布局）: {e}")
    return extras

def build_card(fresh: list, cfg: dict, now: datetime, extras: dict = None) -> dict:
    extras = extras or {}
    front = extras.get("front") or []          # AI 筛选的头条 [{title, summary, img_key?}]
    front_titles = {t["title"] for t in front}
    overview_cap = 3                            # 速览每类条数（头条已承担重要新闻）

    buckets = {c: [] for c in CATEGORY_KEYS}
    other = []
    for it in fresh:
        cat = classify(it)
        if cat and len(buckets[cat]) < overview_cap:
            buckets[cat].append(it)
        elif not cat and len(other) < overview_cap:
            other.append(it)

    for c in buckets:
        buckets[c].sort(key=lambda x: x["dt"] or now, reverse=True)
    other.sort(key=lambda x: x["dt"] or now, reverse=True)

    date_str = f"{now.year}年{now.month}月{now.day}日 周{WEEKDAYS[now.weekday()]}"
    greeting = "早安财经日报" if now.hour < 11 else ("午间财经速递" if now.hour < 17 else "晚间财经要闻")
    icons = {"A股与国内宏观": "📈", "全球市场与美股": "🌍", "行业与公司动态": "🏢"}
    elements = []

    if extras.get("img_key"):
        elements.append({"tag": "img", "img_key": extras["img_key"],
                         "alt": {"tag": "plain_text", "content": "全球主要指数涨跌幅图表"}})
    if extras.get("analysis"):
        elements.append({"tag": "markdown",
                         "content": f"**🤖 AI 今日解读**\n{extras['analysis']}"})
        elements.append({"tag": "hr"})

    # 报纸头版：头条聚焦（图文混排）
    if front:
        by_title = {it["title"]: it for it in fresh}
        elements.append({"tag": "markdown", "content": "**📰 头条聚焦**"})
        for i, t in enumerate(front):
            link = (by_title.get(t["title"]) or {}).get("link", "#")
            entry = f"**[{t['title']}]({link})**"
            if t.get("summary"):
                entry += f"\n{t['summary']}"
            if i < 3 and t.get("img_key"):
                elements.append({"tag": "column_set", "flex_mode": "bisect",
                                 "columns": [
                                     {"tag": "column", "width": "weighted", "weight": 3,
                                      "vertical_align": "top",
                                      "elements": [{"tag": "markdown", "content": entry}]},
                                     {"tag": "column", "width": "weighted", "weight": 2,
                                      "vertical_align": "top",
                                      "elements": [{"tag": "img", "img_key": t["img_key"],
                                                    "alt": {"tag": "plain_text",
                                                            "content": "头条配图"}}]},
                                 ]})
            else:
                elements.append({"tag": "markdown", "content": f"- {entry}"})
        elements.append({"tag": "hr"})

    total = sum(len(v) for v in buckets.values()) + len(other)
    if not elements:
        elements.append({"tag": "markdown", "content": "今日暂无符合条件的新闻（数据源可能无更新）。"})

    for cat, its in buckets.items():
        if not its:
            continue
        lines = [f"**{icons[cat]} {cat}**"]
        for it in its:
            if it["title"] in front_titles:
                continue  # 已在头条展示，速览不重复
            src = f"（{it['source']}）" if it["source"] == "WSJ" else ""
            lines.append(f"- [{it['title']}]({it['link']}){src}")
        if len(lines) > 1:
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
                      "content": f"来源：华尔街见闻 · WSJ ｜ 头条与解读由 DeepSeek 生成，仅供参考，不构成投资建议 ｜ 每日 08:00 自动推送"}],
    })

    issue_no = (now.date() - datetime(2026, 10, 25).date()).days + 1  # 2026-10-25 创刊
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text",
                      "content": f"☕ {greeting} 第{issue_no}期 · {date_str}"},
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
        paths, _meta = build_newspaper(fresh, cfg, now)
        if paths:
            log(f"报纸已生成 {len(paths)} 页: " + ", ".join(Path(p).name for p in paths))
            return
        log(f"报纸生成失败（{_meta}），降级输出卡片 JSON")
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

    # 主路径：报纸图片版（主报 + 期货版）
    paths, _meta = build_newspaper(fresh, cfg, now)
    if paths:
        try:
            total_ok = 0
            for i, path in enumerate(paths, 1):
                img_key = upload_image_bytes(open(path, "rb").read())
                ok = send_all(cfg, {"msg_type": "image", "content": {"image_key": img_key}})
                total_ok += ok
                log(f"第{i}张推送完成（{ok} 个群）")
            if args.preview:
                log(f"预览推送完成（{len(paths)} 张报纸，累计送达 {total_ok} 次），不更新去重状态。")
                return
            if total_ok == 0:
                log("所有群均推送失败，不更新去重状态（下次重推）。")
                sys.exit(2)
            state["seen"].extend(it["link"] for it in items)
            state["last_push"] = now.isoformat()
            save_state(state)
            log(f"报纸推送完成：{len(paths)} 张。")
            return
        except Exception as e:
            log(f"报纸图片上传/推送失败，降级卡片: {e}")
    else:
        log(f"报纸生成失败（{_meta}），降级卡片。")

    # 降级路径：交互卡片
    extras = enrich(fresh, cfg, now)
    card = build_card(fresh, cfg, now, extras)
    ok = send_all(cfg, {"msg_type": "interactive", "card": card})
    if args.preview:
        log(f"预览推送完成（卡片降级，{ok} 个群送达），不更新去重状态。")
        return
    if ok == 0:
        log("所有群均推送失败，不更新去重状态（下次重推）。")
        sys.exit(2)
    state["seen"].extend(it["link"] for it in items)
    state["last_push"] = now.isoformat()
    save_state(state)
    log(f"推送完成（卡片），送达 {ok} 个群。")


if __name__ == "__main__":
    main()
