# -*- coding: utf-8 -*-
"""四页报纸渲染器：头版 / 市场版 / 产业版 / 评论版。"""
import json
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
    for t in layout["top"][:3]:
        yy = pg.text_block(0.05, yy, t["item"]["title"], 0.9, 12, color=INK, fontweight="bold")
        yy -= 0.003
        yy = pg.text_block(0.05, yy, t["summary"], 0.9, 9.8, color="#333333", max_lines=6)
        yy -= 0.012
        pg.rule(yy + 0.006, 0.5, "#dddddd")
        yy -= 0.010
    pg.ax.text(0.5, 0.055, "—— 详见 第2版 全球市场 · 第3版 宏观产业 · 第4版 今日解读 ——",
               fontsize=9.5, color=SUB, ha="center")
    paths.append(pg.save("newspaper_p1.png"))

    # ── P2 全球市场 ──
    pg = Page(plt)
    pg.masthead(now, 2)
    if quotes:
        ax1 = pg.fig.add_axes([0.07, 0.60, 0.40, 0.27])
        draw_bar(ax1, quotes)
        _style_chart_ax(ax1)
    if history:
        ax2 = pg.fig.add_axes([0.53, 0.60, 0.40, 0.27])
        draw_trend(ax2, history)
        _style_chart_ax(ax2)
    pg.rule(0.575, 1.2, "#bbbbbb")
    pg.ax.text(0.05, 0.555, "市场动态", fontsize=13, color=ACC, fontweight="bold", va="top")
    yy = 0.555 - pg.lh(13) - 0.006
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
        for ln in news._wrap(para, pg.chars(0.9, 10.5)):
            yy -= pg.lh(10.5)
            pg.ax.text(0.05, yy, ln, fontsize=10.5, color="#2a2a2a", va="top")
        yy -= 0.012
    yy -= 0.010
    box_top = yy
    n_lines = sum(len(news._wrap(c.lstrip("- "), pg.chars(0.83, 9.6))) for c in layout["comment"][:5])
    box_h = 0.032 + n_lines * pg.lh(9.6) + 0.012
    pg.ax.add_patch(FancyBboxPatch((0.05, box_top - box_h), 0.9, box_h,
                                   boxstyle="round,pad=0.006,rounding_size=0.010",
                                   fc="#f2ede2", ec="#c9b98a", lw=1.0))
    pg.ax.text(0.072, box_top - 0.014, "今日要点与风险提示", fontsize=12.5,
               color=ACC, fontweight="bold", va="top")
    yy = box_top - 0.014 - pg.lh(12.5)
    for c in layout["comment"][:5]:
        for j, ln in enumerate(news._wrap(c.lstrip("- "), pg.chars(0.83, 9.6))[:2]):
            yy -= pg.lh(9.6)
            pg.ax.text(0.072, yy, ("▪ " if j == 0 else "   ") + ln,
                       fontsize=9.6, color="#2a2a2a", va="top")
    pg.rule(box_top - box_h - 0.014, 0.6, "#999999")
    pg.ax.text(0.05, box_top - box_h - 0.030,
               "主编：DeepSeek ｜ 来源：华尔街见闻 · WSJ", fontsize=8.5, color=SUB)
    pg.ax.text(0.95, box_top - box_h - 0.030, "AI 生成内容仅供参考，不构成投资建议",
               fontsize=8.5, color=SUB, ha="right")
    paths.append(pg.save("newspaper_p4.png"))

    return paths
