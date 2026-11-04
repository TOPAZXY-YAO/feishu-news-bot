# -*- coding: utf-8 -*-
"""四页报纸渲染器：头版 / 市场版 / 产业版 / 评论版。"""
import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta

import feishu_news as news

PAGE_W, PAGE_H = 10.0, 12.3
BG, INK, SUB, ACC = "#fbf9f1", "#1a1a1a", "#555555", "#8c1f1f"
UP, DOWN = "#c93030", "#1a9e5c"
LINE_COLORS = ["#8c1f1f", "#1a5fa8", "#d9822b", "#1a9e5c"]

HISTORY_SYMBOLS = [("sh000001", "上证指数"), ("sz399001", "深证成指"),
                   ("sz399006", "创业板指"), ("hkHSI", "恒生指数")]


def fetch_history(datalen: int = 30) -> list:
    """腾讯 K 线接口取指数 30 日收盘价，返回 [{name, dates, closes, chg30}]；失败项跳过。"""
    out = []
    for sym, name in HISTORY_SYMBOLS:
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{datalen},day"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            node = data["data"][sym]
            klines = node.get("day") or node.get("qfqday") or []
            closes = [float(k[2]) for k in klines]
            dates = [k[0] for k in klines]
            if len(closes) >= 5:
                out.append({"name": name, "dates": dates, "closes": closes,
                            "chg30": (closes[-1] / closes[0] - 1) * 100})
        except Exception as e:
            news.log(f"K线 {name} 获取失败（跳过）: {e}")
    return out


def _setup(fig):
    """设置中文字体并返回页面工具。"""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt
    for cand in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei"):
        if any(f.name == cand for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


class Page:
    """一页报纸：画布 + 排版工具（动态游标）。"""

    def __init__(self, plt):
        self.plt = plt
        self.fig = plt.figure(figsize=(PAGE_W, PAGE_H), dpi=150)
        self.fig.patch.set_facecolor(BG)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, 1)
        self.ax.set_ylim(0, 1)
        self.ax.axis("off")

    def chars(self, w_frac, fs):
        return w_frac * PAGE_W * 72 / fs

    def lh(self, fs):
        return fs * 1.45 / (72 * PAGE_H)

    def rule(self, y, lw=1.6, color=INK, x0=0.05, x1=0.95):
        self.ax.plot([x0, x1], [y, y], color=color, lw=lw, solid_capstyle="butt")

    def text_block(self, x, y, text, w_frac, fs, max_lines=99, **kw):
        text = " ".join(str(text).split())  # 折叠换行/连续空白，防 matplotlib 隐式换行导致叠字
        for ln in news._wrap(text, self.chars(w_frac, fs))[:max_lines]:
            y -= self.lh(fs)
            self.ax.text(x, y, ln, fontsize=fs, va="top", **kw)
        return y

    def masthead(self, now, page_no):
        issue_no = (now.date() - datetime(2026, 10, 25).date()).days + 1
        if page_no == 1:
            self.ax.text(0.5, 0.972, "早安财经日报", ha="center", va="center",
                         fontsize=34, fontweight="bold", color=INK)
            self.ax.text(0.05, 0.968, f"第 {issue_no} 期", ha="left", va="center",
                         fontsize=11, color=SUB)
            self.ax.text(0.95, 0.968, now.strftime("%Y年%m月%d日 %A"), ha="right",
                         va="center", fontsize=11, color=SUB)
            self.rule(0.9515, 2.2)
            self.rule(0.9475, 0.6)
        else:
            names = ["头版", "全球市场", "宏观·产业", "今日解读"]
            self.ax.text(0.05, 0.965, "早安财经日报", fontsize=14, fontweight="bold", color=INK)
            self.ax.text(0.95, 0.965, f"第 {issue_no} 期 · 第{page_no}版 {names[page_no-1]}",
                         fontsize=11, color=SUB, ha="right")
            self.ax.text(0.5, 0.965, now.strftime("%Y年%m月%d日 %A"),
                         fontsize=10, color=SUB, ha="center", va="center")
            self.rule(0.9465, 1.6)

    def index_strip(self, quotes):
        y0 = 0.924
        if quotes:
            n = len(quotes)
            for i, q in enumerate(quotes):
                x = 0.055 + i * (0.89 / n)
                color = UP if q["pct"] >= 0 else DOWN
                self.ax.text(x, y0 + 0.011, q["name"], fontsize=8.2, color=SUB, ha="left")
                self.ax.text(x, y0 - 0.009, f"{q['pct']:+.2f}%", fontsize=10.5,
                             color=color, ha="left", fontweight="bold")
        self.rule(0.899, 0.7, "#999999")

    def save(self, name: str) -> str:
        path = str(news.BASE / name)
        self.fig.savefig(path, facecolor=BG)
        self.plt.close(self.fig)
        return path


# ---------- 图表（画到指定 axes） ----------

def draw_bar(ax, quotes):
    qs = sorted(quotes, key=lambda q: q["pct"])
    names = [q["name"] for q in qs]
    pcts = [q["pct"] for q in qs]
    colors = [UP if p >= 0 else DOWN for p in pcts]
    bars = ax.barh(names, pcts, color=colors, height=0.6)
    for bar, pct in zip(bars, pcts):
        off = 0.03 if pct >= 0 else -0.03
        ax.text(pct + off, bar.get_y() + bar.get_height() / 2, f"{pct:+.2f}%",
                va="center", ha="left" if pct >= 0 else "right", fontsize=9, color="#333333")
    ax.axvline(0, color="#888888", lw=0.8)
    ax.set_title("全球主要指数当日涨跌幅 %", fontsize=12, pad=8)
    ax.tick_params(labelsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    xmin, xmax = ax.get_xlim()
    ax.set_xlim(xmin - 0.5, xmax + 0.5)


def draw_trend(ax, history):
    """30 日归一化走势多线图。"""
    for i, h in enumerate(history):
        base = h["closes"][0]
        norm = [(c / base - 1) * 100 for c in h["closes"]]
        ax.plot(range(len(norm)), norm, color=LINE_COLORS[i % len(LINE_COLORS)],
                lw=1.8, label=f"{h['name']} {h['chg30']:+.1f}%")
    ax.axhline(0, color="#888888", lw=0.7)
    ax.set_title("近 30 日走势（期初=0）", fontsize=12, pad=8)
    ax.legend(fontsize=8.5, loc="best", framealpha=0.6)
    ax.grid(True, lw=0.4, alpha=0.4)
    ax.tick_params(labelsize=8)
    n = max(len(h["closes"]) for h in history)
    step = max(1, n // 5)
    ax.set_xticks(range(0, n, step))
    ax.set_xticklabels([history[0]["dates"][i][5:] for i in range(0, n, step)], fontsize=8)


def draw_rank(ax, history):
    """30 日累计涨跌% 横向排行。"""
    hs = sorted(history, key=lambda h: h["chg30"])
    names = [h["name"] for h in hs]
    vals = [h["chg30"] for h in hs]
    colors = [UP if v >= 0 else DOWN for v in vals]
    bars = ax.barh(names, vals, color=colors, height=0.55)
    for bar, v in zip(bars, vals):
        off = 0.15 if v >= 0 else -0.15
        ax.text(v + off, bar.get_y() + bar.get_height() / 2, f"{v:+.1f}%",
                va="center", ha="left" if v >= 0 else "right", fontsize=9.5, color="#333333")
    ax.axvline(0, color="#888888", lw=0.8)
    ax.set_title("近 30 日累计涨跌幅 %", fontsize=12, pad=8)
    ax.tick_params(labelsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    xmin, xmax = ax.get_xlim()
    ax.set_xlim(xmin - 1.2, xmax + 1.2)


def _style_chart_ax(ax):
    ax.set_facecolor("#fdfcf7")
    for sp in ax.spines.values():
        sp.set_color("#cccccc")


def draw_quote_table(pg, x, y, w, quotes, history, title="指数行情速览", first_col="指数"):
    """指数行情表：名称/现价/当日%/30日%。返回结束 y。"""
    hist_map = {h["name"]: h["chg30"] for h in history}
    col_x = [x, x + w * 0.34, x + w * 0.60, x + w * 0.79]
    headers = ["指数", "现价", "当日", "30日"]
    pg.ax.text(x, y, title, fontsize=12.5, color=ACC, fontweight="bold", va="top")
    y -= pg.lh(12.5) + 0.004
    headers[0] = first_col
    for cx, h in zip(col_x, headers):
        pg.ax.text(cx, y, h, fontsize=9.5, color=SUB, fontweight="bold", va="top")
    y -= pg.lh(9.5) + 0.002
    row_h = pg.lh(10) + 0.0018
    from matplotlib.patches import Rectangle
    for i, q in enumerate(quotes):
        if i % 2 == 0:
            pg.ax.add_patch(Rectangle((x - 0.008, y - row_h * 0.85), w + 0.016, row_h,
                                      fc="#f3efe3", ec="none", zorder=1))
        c = UP if q["pct"] >= 0 else DOWN
        h30 = hist_map.get(q["name"])
        c30 = UP if (h30 or 0) >= 0 else DOWN
        z30 = f"{h30:+.1f}%" if h30 is not None else "—"
        pg.ax.text(col_x[0], y, q["name"], fontsize=10, color=INK, va="top", zorder=2)
        pg.ax.text(col_x[1], y, f"{q['price']:,.2f}", fontsize=10, color=INK, va="top", zorder=2)
        pg.ax.text(col_x[2], y, f"{q['pct']:+.2f}%", fontsize=10, color=c, va="top",
                   fontweight="bold", zorder=2)
        pg.ax.text(col_x[3], y, z30, fontsize=10, color=c30, va="top", zorder=2)
        y -= row_h
    return y


# ---------- 四页渲染 ----------

def render_pages(materials, layout, quotes, history, now, lead_img_url="") -> list:
    plt = _setup(None)
    paths = []
    by_no = {m["no"]: m for m in materials}

    # ── P1 头版 ──
    pg = Page(plt)
    pg.masthead(now, 1)
    pg.index_strip(quotes)
    lead = layout["lead"]
    it = lead["item"]
    pg.ax.text(0.05, 0.884, "今日头条", fontsize=11, color=ACC, fontweight="bold", ha="left")
    pg.ax.text(0.125, 0.884, it["title"][:38], fontsize=9, color="#999999", ha="left", va="center")
    y = 0.872
    y = pg.text_block(0.05, y, it["title"], 0.55, 15.5, color=INK, fontweight="bold")
    y -= 0.006
    y = pg.text_block(0.05, y, lead["summary"], 0.55, 10.8, color="#2a2a2a")
    if y > 0.70:
        y = 0.70
    if lead_img_url:
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(news.http_get(lead_img_url, timeout=30)))
            img_h = max(0.10, min(0.17, 0.872 - y - 0.012))
            ax_img = pg.fig.add_axes([0.645, 0.872 - img_h, 0.305, img_h])
            ax_img.imshow(img, aspect="auto")
            ax_img.set_xticks([]); ax_img.set_yticks([])
            for sp in ax_img.spines.values():
                sp.set_color("#cccccc")
        except Exception as e:
            news.log(f"头条配图加载失败: {e}")
    pg.rule(max(y - 0.012, 0.695), 1.0, "#bbbbbb")
    yy = min(y - 0.03, 0.665)
    pg.ax.text(0.05, yy + 0.014, "要闻", fontsize=12.5, color=ACC, fontweight="bold", va="top")
    for t in layout["top"][:4]:
        yy = pg.text_block(0.05, yy, t["item"]["title"], 0.9, 12, color=INK, fontweight="bold")
        yy -= 0.003
        yy = pg.text_block(0.05, yy, t["summary"], 0.9, 9.8, color="#333333", max_lines=6)
        yy -= 0.012
        pg.rule(yy + 0.006, 0.5, "#dddddd")
        yy -= 0.010
    # 更多要闻速览（未上头条/简讯的标题，两栏）
    used = {t["item"]["title"] for t in layout["top"]} | {layout["lead"]["item"]["title"]}
    for key in ("market_briefs", "cn_briefs", "industry_briefs"):
        used |= {b["item"]["title"] for b in layout.get(key, [])}
    more = [it for it in materials if it["title"] not in used][:6]
    if more:
        pg.rule(yy + 0.004, 1.2, "#bbbbbb")
        pg.ax.text(0.05, yy - 0.020, "更多要闻", fontsize=12.5, color=ACC, fontweight="bold", va="top")
        col_w, col_dx = 0.44, 0.47
        rows = (len(more) + 1) // 2
        for i, m in enumerate(more):
            cx = 0.05 + (i % 2) * col_dx
            ry = yy - 0.044 - (i // 2) * pg.lh(10) * 2.3
            for ln in news._wrap(m["title"], pg.chars(col_w, 10))[:2]:
                pg.ax.text(cx, ry, ln, fontsize=10, color=INK, va="top")
                ry -= pg.lh(10)
        yy = yy - 0.044 - rows * pg.lh(10) * 2.3 - 0.010
    if quotes:
        yy = draw_quote_table(pg, 0.05, max(yy - 0.024, 0.16), 0.9, quotes, history)
    pg.ax.text(0.5, 0.048, "—— 详见 第2版 全球市场 · 第3版 宏观产业 · 第4版 今日解读 ——",
               fontsize=9.5, color=SUB, ha="center")
    pg.ax.text(0.5, 0.048, "—— 详见 第2版 全球市场 · 第3版 宏观产业 · 第4版 今日解读 ——",
               fontsize=9.5, color=SUB, ha="center")
    paths.append(pg.save("newspaper_p1.png"))

    # ── P2 全球市场 ──
    pg = Page(plt)
    pg.masthead(now, 2)
    if quotes:
        ax1 = pg.fig.add_axes([0.07, 0.55, 0.40, 0.345])
        draw_bar(ax1, quotes)
        _style_chart_ax(ax1)
    if history:
        ax2 = pg.fig.add_axes([0.53, 0.55, 0.40, 0.345])
        draw_trend(ax2, history)
        _style_chart_ax(ax2)
    pg.rule(0.528, 1.2, "#bbbbbb")
    pg.ax.text(0.05, 0.508, "市场动态", fontsize=13, color=ACC, fontweight="bold", va="top")
    yy = 0.508 - pg.lh(13) - 0.006
    for b in layout.get("market_briefs", [])[:3]:
        yy = pg.text_block(0.05, yy, "▪ " + b["item"]["title"], 0.9, 11.5,
                           color=INK, fontweight="bold")
        yy = pg.text_block(0.05, yy, b["brief"], 0.9, 10, color="#333333", max_lines=5)
        yy -= 0.014
        pg.rule(yy + 0.007, 0.5, "#dddddd")
        yy -= 0.012
    pg.ax.text(0.95, 0.055, "数据来源：腾讯行情 ｜ 走势以期初为基准归一化",
               fontsize=8.5, color=SUB, ha="right")
    paths.append(pg.save("newspaper_p2.png"))

    # ── P3 宏观·产业 ──
    pg = Page(plt)
    pg.masthead(now, 3)
    if history:
        ax3 = pg.fig.add_axes([0.07, 0.62, 0.40, 0.24])
        draw_rank(ax3, history)
        _style_chart_ax(ax3)
    pg.ax.text(0.55, 0.845, "宏观看点", fontsize=13, color=ACC, fontweight="bold", va="top")
    yy = 0.845 - pg.lh(13) - 0.006
    for b in layout.get("cn_briefs", [])[:3]:
        yy = pg.text_block(0.55, yy, "▪ " + b["item"]["title"], 0.40, 10.5,
                           color=INK, fontweight="bold", max_lines=2)
        yy = pg.text_block(0.55, yy, b["brief"], 0.40, 9.5, color="#333333", max_lines=4)
        yy -= 0.012
    # 分隔线跟随宏观看点实际结束位置（左图底 0.62 为下限）
    rule_y = max(yy - 0.018, 0.52)
    pg.rule(rule_y, 1.2, "#bbbbbb")
    pg.ax.text(0.05, rule_y - 0.024, "行业与公司", fontsize=13, color=ACC,
               fontweight="bold", va="top")
    yy = rule_y - 0.024 - pg.lh(13) - 0.006
    ind = layout.get("industry_briefs", [])[:3]
    for b in ind:
        yy = pg.text_block(0.05, yy, "▪ " + b["item"]["title"], 0.9, 11.5,
                           color=INK, fontweight="bold", max_lines=2)
        yy = pg.text_block(0.05, yy, b["brief"], 0.9, 10, color="#333333", max_lines=4)
        yy -= 0.016
    pg.ax.text(0.95, 0.055, "数据来源：腾讯行情", fontsize=8.5, color=SUB, ha="right")
    paths.append(pg.save("newspaper_p3.png"))

    # ── P4 今日解读 ──
    pg = Page(plt)
    pg.masthead(now, 4)
    from matplotlib.patches import FancyBboxPatch
    pg.ax.text(0.05, 0.885, "深度综述", fontsize=15, color=ACC, fontweight="bold", va="top")
    yy = 0.885 - pg.lh(15) - 0.008
    for para in layout.get("review", "").split("\n\n"):
        para = " ".join(para.split())
        for ln in news._wrap(para, pg.chars(0.9, 10.5)):
            yy -= pg.lh(10.5)
            pg.ax.text(0.05, yy, ln, fontsize=10.5, color="#2a2a2a", va="top")
        yy -= 0.012
    yy -= 0.010
    box_top = yy
    comments = [" ".join(c.split()) for c in layout["comment"][:6]]
    n_lines = sum(len(news._wrap(c.lstrip("- "), pg.chars(0.83, 9.6))) for c in comments)
    box_h = 0.032 + n_lines * pg.lh(9.6) + 0.012
    pg.ax.add_patch(FancyBboxPatch((0.05, box_top - box_h), 0.9, box_h,
                                   boxstyle="round,pad=0.006,rounding_size=0.010",
                                   fc="#f2ede2", ec="#c9b98a", lw=1.0))
    pg.ax.text(0.072, box_top - 0.014, "今日要点与风险提示", fontsize=12.5,
               color=ACC, fontweight="bold", va="top")
    yy = box_top - 0.014 - pg.lh(12.5)
    for c in layout["comment"][:6]:
        for j, ln in enumerate(news._wrap(c.lstrip("- "), pg.chars(0.83, 9.6))[:2]):
            yy -= pg.lh(9.6)
            pg.ax.text(0.072, yy, ("▪ " if j == 0 else "   ") + ln,
                       fontsize=9.6, color="#2a2a2a", va="top")
    cal = layout.get("calendar", [])[:5]
    if cal:
        yy = box_top - box_h - 0.022
        pg.ax.text(0.05, yy, "后续关注日历", fontsize=12.5, color=ACC, fontweight="bold", va="top")
        yy -= pg.lh(12.5) + 0.004
        for e in cal:
            when = " ".join(str(e.get("when", "")).split())[:16]
            event = " ".join(str(e.get("event", "")).split())
            pg.ax.text(0.05, yy, when, fontsize=10, color=ACC, va="top", fontweight="bold")
            for ln in news._wrap(event, pg.chars(0.74, 10))[:2]:
                pg.ax.text(0.21, yy, ln, fontsize=10, color="#2a2a2a", va="top")
                yy -= pg.lh(10)
            yy -= 0.005
    pg.rule(0.062, 0.6, "#999999")
    pg.ax.text(0.05, 0.044, "主编：DeepSeek ｜ 来源：华尔街见闻 · WSJ", fontsize=8.5, color=SUB)
    pg.ax.text(0.95, 0.044, "AI 生成内容仅供参考，不构成投资建议", fontsize=8.5, color=SUB, ha="right")
    paths.append(pg.save("newspaper_p4.png"))

    return paths


# ---------- 期货版 ----------

FUTURES_SYMBOLS = [
    ("RB0", "螺纹钢"), ("HC0", "热卷"), ("I0", "铁矿石"),
    ("SC0", "原油"),
    ("AU0", "沪金"), ("AG0", "沪银"), ("CU0", "沪铜"), ("AL0", "沪铝"),
    ("M0", "豆粕"), ("C0", "玉米"),
]


def fetch_futures_quotes() -> list:
    """新浪期货实时行情（主力连续），涨跌以昨收盘为基准。"""
    codes = ",".join("nf_" + s for s, _ in FUTURES_SYMBOLS)
    req = urllib.request.Request("https://hq.sinajs.cn/list=" + codes, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    text = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="replace")
    quotes = []
    name_map = {s: n for s, n in FUTURES_SYMBOLS}
    for line in text.strip().splitlines():
        if "=" not in line:
            continue
        code = line.split("=")[0].split("_")[-1]
        f = line.split("=", 1)[1].strip().strip('"').split(",")
        if len(f) < 18 or not f[0] or code not in name_map:
            continue
        try:
            last, prev = float(f[7]), float(f[10]) or float(f[9])
            if last <= 0 or prev <= 0:
                continue
            quotes.append({"name": name_map[code], "price": last,
                           "pct": (last / prev - 1) * 100})
        except (ValueError, ZeroDivisionError):
            continue
    if not quotes:
        raise RuntimeError("期货行情接口未返回有效数据")
    return quotes


def fetch_futures_history(datalen: int = 30) -> list:
    """新浪期货日K线（全量返回，取最近 datalen 天）。"""
    out = []
    for sym, name in FUTURES_SYMBOLS:
        try:
            url = (f"https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                   f"var%20t=/InnerFuturesNewService.getDailyKLine?symbol={sym}")
            raw = urllib.request.urlopen(urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}),
                timeout=15).read().decode("utf-8", errors="replace")
            m = re.search(r"\[.*\]", raw, re.S)
            if not m:
                continue
            data = json.loads(m.group(0))
            dates = [d["d"] for d in data][-datalen:]
            closes = [float(d["c"]) for d in data][-datalen:]
            if len(closes) >= 5:
                out.append({"name": name, "closes": closes, "dates": dates,
                            "chg30": (closes[-1] / closes[0] - 1) * 100})
        except Exception as e:
            news.log(f"期货K线 {name} 失败（跳过）: {e}")
    return out


def ai_futures_comment(quotes: list, headlines: list) -> str:
    """期货分析师点评：按板块写当日逻辑 + 风险提示。失败返回空串。"""
    idx_line = "；".join(f"{q['name']}{q['pct']:+.2f}%" for q in quotes)
    prompt = (
        "你是资深期货分析师，为报纸期货版写「期货市场点评」。基于以下国内商品期货主力合约"
        "行情与财经素材，按板块（黑色、能化、贵金属、有色、农产品，仅写当日行情有关联的板块）"
        "各写 1 条点评（每条≤50字：涨跌逻辑+关注点），最后一条为风险提示（≤40字）。"
        "每条以 - 开头，直接输出 JSON {\"comment\": [...]}，无其他文字。行情数字可引用，"
        "但逻辑推断需注明是判断而非事实。\n\n"
        f"期货行情：{idx_line}\n相关财经要闻：\n"
        + "\n".join(f"- {t}" for t in headlines[:15])
    )
    try:
        text = news.llm_complete([{"role": "user", "content": prompt}],
                                 max_tokens=800, temperature=0.35)
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return [str(x) for x in data.get("comment", [])]
    except Exception as e:
        news.log(f"期货点评生成失败（跳过）: {e}")
        return ""


def render_futures_page(now, quotes, history, comment, headlines):
    """渲染期货版整页，返回 PNG 路径。布局：条形图→点评框→双走势→行情表→页脚。"""
    from matplotlib.patches import FancyBboxPatch
    plt = _setup(None)
    pg = Page(plt)
    issue_no = (now.date() - datetime(2026, 10, 25).date()).days + 1
    pg.ax.text(0.5, 0.972, "早安财经日报 · 期货版", ha="center", va="center",
               fontsize=30, fontweight="bold", color=INK)
    pg.ax.text(0.05, 0.968, f"第 {issue_no} 期", ha="left", va="center", fontsize=11, color=SUB)
    pg.ax.text(0.95, 0.968, now.strftime("%Y年%m月%d日 %A"), ha="right", va="center",
               fontsize=11, color=SUB)
    pg.rule(0.9515, 2.2)
    pg.rule(0.9475, 0.6)

    # 当日涨跌幅条形图（全宽）
    qs = sorted(quotes, key=lambda q: q["pct"])
    ax1 = pg.fig.add_axes([0.07, 0.715, 0.86, 0.215])
    bars = ax1.barh([q["name"] for q in qs], [q["pct"] for q in qs],
                    color=[UP if q["pct"] >= 0 else DOWN for q in qs], height=0.6)
    for bar, q in zip(bars, qs):
        off = 0.04 if q["pct"] >= 0 else -0.04
        ax1.text(q["pct"] + off, bar.get_y() + bar.get_height() / 2, f"{q['pct']:+.2f}%",
                 va="center", ha="left" if q["pct"] >= 0 else "right", fontsize=8.5,
                 color="#333333")
    ax1.axvline(0, color="#888888", lw=0.8)
    ax1.set_title("商品期货主力合约当日涨跌幅 %", fontsize=12, pad=6)
    ax1.tick_params(labelsize=8.5)
    ax1.spines[["top", "right"]].set_visible(False)
    xmin, xmax = ax1.get_xlim()
    ax1.set_xlim(xmin - 0.35, xmax + 0.35)
    _style_chart_ax(ax1)

    # AI 点评框
    box_top, box_h = 0.700, 0.150
    pg.ax.add_patch(FancyBboxPatch((0.05, box_top - box_h), 0.9, box_h,
                                   boxstyle="round,pad=0.006,rounding_size=0.010",
                                   fc="#eef2ea", ec="#9ab88a", lw=1.0))
    pg.ax.text(0.072, box_top - 0.014, "期货市场点评", fontsize=12.5,
               color="#2e6b45", fontweight="bold", va="top")
    yy2 = box_top - 0.014 - pg.lh(12.5)
    for c in comment[:6]:
        c = " ".join(str(c).split())
        for j, ln in enumerate(news._wrap(c.lstrip("- "), pg.chars(0.83, 9.6))[:2]):
            yy2 -= pg.lh(9.6)
            pg.ax.text(0.072, yy2, ("▪ " if j == 0 else "   ") + ln,
                       fontsize=9.6, color="#2a2a2a", va="top")

    # 走势双图
    hist_map = {h["name"]: h for h in history}
    picks = [hist_map[n] for n in ("螺纹钢", "沪金") if n in hist_map]
    for i, h in enumerate(picks[:2]):
        axc = pg.fig.add_axes([0.07 + i * 0.465, 0.295, 0.40, 0.190])
        base = h["closes"][0]
        norm = [(c / base - 1) * 100 for c in h["closes"]]
        axc.plot(range(len(norm)), norm, color=LINE_COLORS[i % len(LINE_COLORS)], lw=1.8)
        axc.axhline(0, color="#888888", lw=0.7)
        axc.set_title(f"{h['name']} 近30日走势（累计 {h['chg30']:+.1f}%）", fontsize=11, pad=6)
        axc.grid(True, lw=0.4, alpha=0.4)
        axc.tick_params(labelsize=8)
        _style_chart_ax(axc)
        axc.set_xticks(range(0, len(norm), max(1, len(norm) // 5)))
        axc.set_xticklabels([h["dates"][j][5:] for j in range(0, len(norm),
                             max(1, len(norm) // 5))], fontsize=7.5)

    # 行情表（10 品种）
    y = draw_quote_table(pg, 0.05, 0.265, 0.9, quotes, history,
                         title="主力合约行情速览", first_col="品种")

    pg.rule(0.052, 0.6, "#999999")
    pg.ax.text(0.05, 0.036, "行情来源：新浪财经 ｜ 点评：DeepSeek ｜ 主力连续合约，涨跌以昨收盘为基准",
               fontsize=8.5, color=SUB)
    pg.ax.text(0.95, 0.036, "期市有风险，入市需谨慎", fontsize=8.5, color=SUB, ha="right")

    path = str(news.BASE / "newspaper_futures.png")
    pg.fig.savefig(path, facecolor=BG)
    plt.close(pg.fig)
    return path