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
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feishu_news as news

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "paper_state.json"
WEEKDAYS = "一二三四五六日"

ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")
LLM_MODELS = ["glm-4-flash-250414", "glm-4.7-flash", "glm-4.5-flash"]  # 与对话机器人同款回退链
SUMMARY_BUDGET_SECONDS = 240  # 概括阶段总预算，超时后剩余条目不再概括

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

JOURNAL_FEEDS = {f["name"] for f in FEEDS if f["section"] == "journal"}

# 中文顶刊：官网无 RSS，直接解析首页服务端渲染的当期目录或调用官方 API
# （中国工业经济=博渊星辰CMS，取 id 最大连续段即当期；数量经济=勤云CMS，file_no=YYYYMMNN 取最大年月组；
#   中国农村经济=ajcass 新平台 Vue CMS，官方 JSON API 可拿目录+逐篇摘要）
CHINESE_SCRAPERS = [
    {"name": "经济研究", "kind": "ajcass_vue", "site": "https://erj.ajcass.com",
     "journal_id": "201803050001", "cap": 8,
     "detail_route": "/#/issue?id={id}&year={year}&issue={issue}"},
    {"name": "管理世界", "kind": "mwm", "url": "http://www.mwm.net.cn", "cap": 8},
    {"name": "中国农村经济", "kind": "ajcass_vue", "site": "https://zgncjj.ajcass.com",
     "journal_id": "201606270007", "cap": 8,
     "detail_route": "/#/issueDetail?contentId={id}&year={year}&issue={issue}"},
    {"name": "中国工业经济", "url": "https://ciejournal.ajcass.com", "kind": "cie", "cap": 8},
    {"name": "数量经济技术经济研究", "url": "https://www.jqte.net", "kind": "jqte", "cap": 8},
]

SECTION_META = [
    ("nber", "NBER 工作论文", "📚"),
    ("journal", "期刊最新发表", "📖"),
    ("cnjournal", "中文顶刊", "📜"),
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


# ---------- AI 论文概括 ----------

def zhipu_summarize(title: str, abstract: str = "", zh: bool = False) -> str:
    """用智谱免费模型把论文概括成一句中文（45字内）。失败返回空串，不影响推送。"""
    if not ZHIPU_KEY:
        return ""
    if abstract:
        prompt = ("你是经济学研究助理。用一句不超过45字的中文概括这篇论文研究什么、核心发现是什么。"
                  "直接输出概括本身，不要前缀、引号或评论。\n"
                  f"标题：{title}\n摘要：{abstract[:1500]}")
    elif zh:
        prompt = ("你是经济学研究助理。用一句不超过35字的中文大白话说明这篇中文论文研究什么问题、"
                  "为什么重要，不要照抄标题字面。直接输出概括本身。\n"
                  f"标题：{title}")
    else:
        prompt = ("你是经济学研究助理。仅根据标题用一句不超过40字的中文概括这篇论文的研究方向。"
                  "无法确定具体结论时只说大致主题，不要编造。直接输出概括本身。\n"
                  f"标题：{title}")
    last_err = None
    for model in LLM_MODELS:
        for attempt in range(2):
            try:
                body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3, "max_tokens": 120,
                        "thinking": {"type": "disabled"}}
                req = urllib.request.Request(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {ZHIPU_KEY}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                out = (data["choices"][0]["message"].get("content") or "").strip()
                return re.sub(r"\s+", " ", out)[:80]
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code == 401:
                    news.log(f"概括失败：智谱 Key 无效")
                    return ""
                if e.code == 429 and attempt < 1:
                    time.sleep(2)
                    continue
                break
            except Exception as e:
                last_err = e
                break
    news.log(f"概括失败（{title[:30]}…）：{last_err}")
    return ""


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

# ---------- 中文期刊抓取 ----------

def scrape_cie(feed: dict) -> list:
    """中国工业经济（博渊星辰CMS）：首页当期目录是 id 最大且连续的 /Magazine/Show 链接块。"""
    html = news.http_get(feed["url"]).decode("utf-8", errors="replace")
    pairs = re.findall(r'/Magazine/Show\?id=(\d+)[^>]*>\s*([^<]{5,80})', html)
    if not pairs:
        raise RuntimeError("首页未找到文章链接（结构可能已变更）")
    max_id = max(int(a) for a, _ in pairs)
    items, seen = [], set()
    for aid, title in pairs:
        if int(aid) >= max_id - 40 and aid not in seen:
            seen.add(aid)
            t = re.sub(r"\s+", " ", title).strip()
            if len(t) >= 8:
                items.append({"title": t, "link": f"{feed['url']}/Magazine/Show?id={aid}",
                              "dt": None, "desc": ""})
    return items[:feed["cap"]]


def scrape_jqte(feed: dict) -> list:
    """数量经济技术经济研究（勤云CMS）：file_no=YYYYMMNN，取最大年月组即当期目录。"""
    html = news.http_get(feed["url"]).decode("utf-8", errors="replace")
    pairs = re.findall(r'view_abstract\.aspx\?file_no=(\d+)[^"\']*["\'][^>]*>\s*([^<]{5,80})', html)
    if not pairs:
        raise RuntimeError("首页未找到文章链接（结构可能已变更）")
    latest = max(f[:6] for f, _ in pairs)  # YYYYMM
    items, seen = [], set()
    for fno, title in pairs:
        if fno[:6] == latest and fno not in seen:
            seen.add(fno)
            t = re.sub(r"\s+", " ", title).strip()
            if len(t) >= 8:
                items.append({"title": t,
                              "link": f"{feed['url']}/reader/view_abstract.aspx?file_no={fno}&flag=1",
                              "dt": None, "desc": ""})
    return items[:feed["cap"]]


def scrape_ajcass_vue(feed: dict) -> list:
    """ajcass 新平台（如中国农村经济）：官方 JSON API，当期目录 + 逐篇摘要。"""
    api = "https://api.ajcass.com/api/SiteWebApi"
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json",
               "Referer": feed["site"] + "/"}
    req = urllib.request.Request(
        f"{api}/GetCurrentPeriodMutiChannel?JournalID={feed['journal_id']}", headers=headers)
    data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    if data.get("code") != 200:
        raise RuntimeError(f"目录接口返回 {data.get('code')}")
    year, issue = data["data"]["year"], data["data"]["issue"]
    items = []

    def walk(chs):  # 栏目是嵌套树（专题栏目下还有子栏目），递归收集
        for ch in chs or []:
            for it in ch.get("issueInfoList") or []:
                t = re.sub(r"\s+", " ", it.get("title") or "").strip()
                if len(t) >= 8:
                    items.append({"title": t, "id": it["id"],
                                  "authors": (it.get("authors") or "").strip()})
            walk(ch.get("channels"))

    walk(data["data"].get("channels"))
    items = items[:feed["cap"]]
    for it in items:
        try:
            body = {"channelId": 0, "contentId": it["id"], "dataShowType": 1,
                    "dataSourceType": 3, "issue": 0, "year": 0,
                    "JournalID": int(feed["journal_id"])}  # 必须数字类型，字符串会 400
            req = urllib.request.Request(f"{api}/GetContentInfo",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers=headers)
            resp = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
            abs_raw = ((resp.get("data") or {}).get("issueContentInfoResult") or {}).get("abstract") or ""
            it["desc"] = re.sub(r"<[^>]+>", "", abs_raw).strip()
        except Exception:
            it["desc"] = ""
        time.sleep(0.2)
    return [{"title": it["title"],
             "link": feed["site"] + feed["detail_route"].format(id=it["id"], year=year, issue=issue),
             "dt": None, "desc": it["desc"], "authors": it["authors"]}
            for it in items]


def scrape_mwm(feed: dict) -> list:
    """管理世界（mwm.net.cn，仅 http 可达）：本期要目列表 + 文章页摘要。"""
    base = feed["url"].rstrip("/")
    html = news.http_get(base + "/web/bqym").decode("utf-8", errors="replace")
    arts = re.findall(r'/web/xq\?leafId=(\d+)&amp;docId=(\d+)[^>]*>\s*([^<]{5,80})', html)
    if not arts:
        raise RuntimeError("本期要目页未找到文章链接（结构可能已变更）")
    items = []
    for lf, did, title in arts[:feed["cap"]]:
        title = re.sub(r"\s+", " ", title).strip()
        item = {"title": title, "link": f"{base}/web/xq?leafId={lf}&docId={did}",
                "dt": None, "desc": "", "authors": ""}
        try:
            art = news.http_get(item["link"]).decode("utf-8", errors="replace")
            m = re.search(r"作者：\s*([^<]{2,40})", art)
            if m:
                item["authors"] = m.group(1).strip()
            m = re.search(r"摘要：</strong>(.{50,2500}?)</span>", art, re.S)
            if m:
                item["desc"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        except Exception:
            pass
        items.append(item)
        time.sleep(0.2)
    return items


SCRAPE_FUNCS = {"cie": scrape_cie, "jqte": scrape_jqte, "ajcass_vue": scrape_ajcass_vue, "mwm": scrape_mwm}


def fetch_all_papers(cn_local: bool = False) -> list:
    all_items = []
    scrapers = CHINESE_SCRAPERS
    if cn_local:
        # 本地通道：只抓海外机房被墙、需要中国网络的两本刊
        scrapers = [s for s in CHINESE_SCRAPERS if s["kind"] in ("mwm", "jqte")]
    else:
        for feed in FEEDS:
            try:
                items = news.parse_feed(news.http_get(feed["url"]))
                for it in items:
                    it["section"] = feed["section"]
                    it["cap"] = feed["cap"]
                    it["src"] = feed["name"]
                    it["zh"] = False
                all_items.extend(items)
                news.log(f"源 {feed['name']}: 获取 {len(items)} 条")
            except Exception as e:
                news.log(f"源 {feed['name']} 失败（跳过）: {e}")
    for feed in scrapers:
        try:
            items = SCRAPE_FUNCS[feed["kind"]](feed)
            for it in items:
                it["section"] = "cnjournal"
                it["cap"] = feed["cap"]
                it["src"] = feed["name"]
                it["zh"] = True
            all_items.extend(items)
            news.log(f"源 {feed['name']}: 获取 {len(items)} 条（当期目录）")
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
        authors = (it.get("authors") or "").strip()  # ajcass_vue API 直接提供作者
        abstract = clean_title(it.get("desc") or "")
        if it["section"] == "nber":
            title, authors = split_nber(title)
        elif it["src"] in JOURNAL_FEEDS:
            # Elsevier 的 description 是元数据：没有摘要，但含作者，提出来用
            m = re.search(r"Author\(s\):\s*([^<]+)", it.get("desc") or "")
            if m:
                authors = m.group(1).strip()
            abstract = ""
        kept_links.add(link)
        sec = it["section"]
        if feed_counts.get(it["src"], 0) < it["cap"]:
            feed_counts[it["src"]] = feed_counts.get(it["src"], 0) + 1
            sections[sec].append({"title": title, "link": link,
                                  "authors": authors, "abstract": abstract,
                                  "zh": it.get("zh", False)})

    # AI 概括阶段：只概括入选条目，带总时间预算，超时/失败自动降级为无概括
    if ZHIPU_KEY:
        deadline = time.time() + SUMMARY_BUDGET_SECONDS
        n_sum = 0
        for key, _, _ in SECTION_META:
            for item in sections[key]:
                if time.time() > deadline:
                    news.log("概括阶段超预算，剩余条目跳过概括")
                    break
                item["summary"] = zhipu_summarize(item["title"], item["abstract"],
                                                  zh=item.get("zh", False))
                if item["summary"]:
                    n_sum += 1
                time.sleep(0.3)  # 轻微间隔，降低免费档限流概率
        news.log(f"AI 概括完成：{n_sum} 条")

    date_str = f"{now.year}年{now.month}月{now.day}日 周{WEEKDAYS[now.weekday()]}"
    elements, total = [], 0
    for key, name, icon in SECTION_META:
        items = sections[key]
        if not items:
            continue
        total += len(items)
        lines = [f"**{icon} {name}**"]
        for item in items:
            line = f"- [{item['title']}]({item['link']})"
            if item.get("authors"):
                line += f"（{item['authors']}）"
            if item.get("summary"):
                line += f"\n  ↳ {item['summary']}"
            lines.append(line)
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        elements.append({"tag": "hr"})

    if not elements:
        elements = [{"tag": "markdown", "content": "今日各源均无新论文。"}]
    elif elements[-1].get("tag") == "hr":
        elements.pop()

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": f"共 {total} 篇 ｜ 来源：NBER · Elsevier · 中文顶刊 · AgEcon · arXiv ｜ ↳ 为 AI 概括（智谱GLM）｜ 每日 08:30 自动推送"}],
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
    ap.add_argument("--cn-local", action="store_true",
                    help="本地通道：仅抓海外被墙的中文刊（管理世界/数量经济），独立状态文件")
    args = ap.parse_args()

    global STATE_FILE, ZHIPU_KEY
    if args.cn_local:
        STATE_FILE = BASE / "paper_state_local.json"
        env_file = BASE / "local_env.json"  # 本地凭证（gitignore，不入库）
        if env_file.exists():
            with open(env_file, encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    os.environ.setdefault(k, str(v))
        ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")
        news.log("本地通道模式：管理世界 + 数量经济技术经济研究")

    webhook = os.environ.get("PAPER_WEBHOOK_URL", "")
    secret = os.environ.get("PAPER_SECRET", "")
    now = datetime.now(timezone(timedelta(hours=8)))

    if args.dry_run:
        items = fetch_all_papers(cn_local=args.cn_local)
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

    items = fetch_all_papers(cn_local=args.cn_local)
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
