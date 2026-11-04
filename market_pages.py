# -*- coding: utf-8 -*-
"""扩展版面渲染器：加密货币版 / 全球国债版 / 期货版（独立于 newspaper_pages 的四页渲染）。"""
import json
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import feishu_news as news

BG, INK, SUB, ACC = "#fbf9f1", "#1a1a1a", "#555555", "#8c1f1f"
UP, DOWN = "#c93030", "#1a9e5c"
LINE_COLORS = ["#8c1f1f", "#1a5fa8", "#d9822b", "#1a9e5c"]
PAGE_W, PAGE_H = 10.0, 12.3


def _setup():
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
        text = " ".join(str(text).split())
        for ln in news._wrap(text, self.chars(w_frac, fs))[:max_lines]:
            y -= self.lh(fs)
            self.ax.text(x, y, ln, fontsize=fs, va="top", **kw)
        return y

    def masthead(self, now, edition):
        issue_no = (now.date() - datetime(2026, 10, 25).date()).days + 1
        title = "早安财经日报" + (f" · {edition}" if edition else "")
        self.ax.text(0.5, 0.972, title, ha="center", va="center",
                     fontsize=28 if edition else 34, fontweight="bold", color=INK)
        self.ax.text(0.05, 0.968, f"第 {issue_no} 期", ha="left", va="center",
                     fontsize=11, color=SUB)
        self.ax.text(0.95, 0.968, now.strftime("%Y年%m月%d日 %A"), ha="right",
                     va="center", fontsize=11, color=SUB)
        self.rule(0.9515, 2.2)
        self.rule(0.9475, 0.6)

    def save(self, name):
        path = str(news.BASE / name)
        self.fig.savefig(path, facecolor=BG)
        self.plt.close(self.fig)
        return path


def _style_chart_ax(ax):
    ax.set_facecolor("#fdfcf7")
    for sp in ax.spines.values():
        sp.set_color("#cccccc")


def draw_quote_table(pg, x, y, w, rows, title="行情速览", first_col="品种",
                     third_header="当日", fourth_header="30日", show_fourth=True):
    """通用行情表：品种/现价/当日%（可选第四列：30日% 或 24h% 等）。返回结束 y。"""
    col_x = [x, x + w * 0.34, x + w * 0.60, x + w * 0.79]
    headers = [first_col, "现价", third_header, fourth_header]
    if not show_fourth:
        col_x = [x, x + w * 0.48, x + w * 0.76]
        headers = headers[:3]
    pg.ax.text(x, y, title, fontsize=12.5, color=ACC, fontweight="bold", va="top")
    y -= pg.lh(12.5) + 0.004
    for cx, h in zip(col_x, headers):
        pg.ax.text(cx, y, h, fontsize=9.5, color=SUB, fontweight="bold", va="top")
    y -= pg.lh(9.5) + 0.002
    row_h = pg.lh(10) + 0.0018
    from matplotlib.patches import Rectangle
    for i, r in enumerate(rows):
        if i % 2 == 0:
            pg.ax.add_patch(Rectangle((x - 0.008, y - row_h * 0.85), w + 0.016, row_h,
                                      fc="#f3efe3", ec="none", zorder=1))
        c = UP if r["pct"] >= 0 else DOWN
        pg.ax.text(col_x[0], y, r["name"], fontsize=10, color=INK, va="top", zorder=2)
        pg.ax.text(col_x[1], y, f"{r['price']:,.2f}", fontsize=10, color=INK, va="top", zorder=2)
        pg.ax.text(col_x[2], y, f"{r['pct']:+.2f}%", fontsize=10, color=c, va="top",
                   fontweight="bold", zorder=2)
        if show_fourth:
            pg.ax.text(col_x[3], y, r.get("extra", "—"), fontsize=10, color=c,
                       va="top", zorder=2)
        y -= row_h
    return y


def _comment_box(pg, x, y_top, comment, box_color="#f2ede2", edge="#c9b98a",
                 head_color=ACC, title="点评", max_items=6, w=0.9, chars_scale=0.83):
    from matplotlib.patches import FancyBboxPatch
    comments = [" ".join(str(c).split()) for c in comment[:max_items]]
    n_lines = sum(len(news._wrap(c.lstrip("- "), pg.chars(w - 0.07, 9.6))) for c in comments)
    box_h = 0.030 + n_lines * pg.lh(9.6) + 0.012
    pg.ax.add_patch(FancyBboxPatch((x, y_top - box_h), w, box_h,
                                   boxstyle="round,pad=0.006,rounding_size=0.010",
                                   fc=box_color, ec=edge, lw=1.0))
    pg.ax.text(x + 0.022, y_top - 0.013, title, fontsize=12.5, color=head_color,
               fontweight="bold", va="top")
    yy = y_top - 0.013 - pg.lh(12.5)
    for c in comments:
        for j, ln in enumerate(news._wrap(c.lstrip("- "), pg.chars(w - 0.07, 9.6))[:3]):
            yy -= pg.lh(9.6)
            pg.ax.text(x + 0.022, yy, ("▪ " if j == 0 else "   ") + ln,
                       fontsize=9.6, color="#2a2a2a", va="top")
    return y_top - box_h


# ══════════════ 加密货币版 ══════════════

CRYPTO_IDS = [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"),
              ("binancecoin", "BNB"), ("ripple", "XRP"), ("dogecoin", "DOGE"),
              ("cardano", "ADA"), ("tron", "TRX")]


def _cg_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception as e:
            last_err = e
            time.sleep(3)
    raise last_err


def fetch_crypto_quotes() -> list:
    data = _cg_get("https://api.coingecko.com/api/v3/simple/price?ids="
                   + ",".join(c for c, _ in CRYPTO_IDS)
                   + "&vs_currencies=usd&include_24hr_change=true")
    quotes = []
    for cid, name in CRYPTO_IDS:
        v = data.get(cid)
        if v and v.get("usd"):
            quotes.append({"name": name, "price": float(v["usd"]),
                           "pct": float(v.get("usd_24h_change") or 0)})
    if not quotes:
        raise RuntimeError("CoinGecko 未返回有效数据")
    return quotes


def fetch_crypto_history(cid: str) -> list:
    data = _cg_get(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart"
                   "?vs_currency=usd&days=30&interval=daily")
    return [float(p[1]) for p in data["prices"]]


def ai_crypto_comment(quotes: list, headlines: list) -> list:
    qline = "；".join(f"{q['name']} ${q['price']:,.2f}（24h {q['pct']:+.2f}%）" for q in quotes)
    prompt = (
        "你是加密市场分析师，为报纸加密货币版写「加密市场点评」。基于行情与财经素材，"
        "写 4-5 条要点（BTC/ETH/山寨币/资金面等角度，每条≤45字，说明涨跌逻辑与关注点）"
        "+ 最后一条风险提示（≤40字）。每条以 - 开头。直接输出 JSON {\"comment\": [...]}"
        "，无其他文字。不确定处注明是判断。\n\n"
        f"行情：{qline}\n相关财经要闻：" + "；".join(headlines[:15])
    )
    try:
        text = news.llm_complete([{"role": "user", "content": prompt}],
                                 max_tokens=700, temperature=0.35)
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return [str(x) for x in data.get("comment", [])]
    except Exception as e:
        news.log(f"加密点评生成失败（跳过）: {e}")
        return []


def render_crypto_page(now, quotes, btc_hist, eth_hist, comment):
    plt = _setup()
    pg = Page(plt)
    pg.masthead(now, "加密货币版")

    qs = sorted(quotes, key=lambda q: q["pct"])
    ax1 = pg.fig.add_axes([0.07, 0.665, 0.86, 0.235])
    ax1.barh([q["name"] for q in qs], [q["pct"] for q in qs],
             color=[UP if q["pct"] >= 0 else DOWN for q in qs], height=0.6)
    for q in qs:
        off = 0.12 if q["pct"] >= 0 else -0.12
        ax1.text(q["pct"] + off, qs.index(q), f"{q['pct']:+.2f}%",
                 va="center", ha="left" if q["pct"] >= 0 else "right",
                 fontsize=8.5, color="#333333")
    ax1.axvline(0, color="#888888", lw=0.8)
    ax1.set_title("主流币 24 小时涨跌幅 %", fontsize=12, pad=6)
    ax1.tick_params(labelsize=9)
    ax1.spines[["top", "right"]].set_visible(False)
    xmin, xmax = ax1.get_xlim()
    ax1.set_xlim(xmin - 0.8, xmax + 0.8)
    _style_chart_ax(ax1)

    for i, (h, nm) in enumerate([(btc_hist, "BTC"), (eth_hist, "ETH")]):
        if not h:
            continue
        axc = pg.fig.add_axes([0.07 + i * 0.465, 0.335, 0.40, 0.235])
        base = h[0]
        norm = [(c / base - 1) * 100 for c in h]
        axc.plot(range(len(norm)), norm, color=LINE_COLORS[i % len(LINE_COLORS)], lw=1.8)
        axc.axhline(0, color="#888888", lw=0.7)
        axc.set_title(f"{nm} 近30日走势（累计 {norm[-1]:+.1f}%）", fontsize=11, pad=6)
        axc.grid(True, lw=0.4, alpha=0.4)
        axc.tick_params(labelsize=8)
        _style_chart_ax(axc)

    # 底部并排：点评框（左）+ 行情表（右）
    if comment:
        _comment_box(pg, 0.05, 0.272, comment,
                     box_color="#f0edf5", edge="#8a7ab8",
                     head_color="#5b4a9e", title="加密市场点评", w=0.44)
    rows = [{"name": q["name"], "price": q["price"], "pct": q["pct"]} for q in quotes]
    draw_quote_table(pg, 0.55, 0.272, 0.40, rows,
                     title="行情速览（USD）", first_col="币种",
                     fourth_header="24h", show_fourth=False)

    pg.rule(0.052, 0.6, "#999999")
    pg.ax.text(0.05, 0.036, "行情来源：CoinGecko（USD） ｜ 点评：DeepSeek", fontsize=8.5, color=SUB)
    pg.ax.text(0.95, 0.036, "加密资产波动剧烈，请务必谨慎", fontsize=8.5, color=SUB, ha="right")
    return pg.save("newspaper_crypto.png")


# ══════════════ 全球国债版 ══════════════

BOND_SYMBOLS = [("US10Y", "美国"), ("DE10Y", "德国"), ("JP10Y", "日本"),
                ("GB10Y", "英国"), ("FR10Y", "法国"), ("CN10Y", "中国")]


def fetch_bond_yields() -> list:
    syms = "|".join(s for s, _ in BOND_SYMBOLS)
    url = ("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
           f"?symbols={syms}&requestMethod=itv&noform=1&partnerId=2&fund=1"
           "&exthrs=1&output=json&events=1")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = {}
    for attempt in range(3):
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            break
        except Exception as e:
            if attempt == 2:
                raise
            news.log(f'CNBC retry {attempt + 1}: {e}')
            time.sleep(3)
    name_map = {s: n for s, n in BOND_SYMBOLS}
    out = []
    for item in data["FormattedQuoteResult"]["FormattedQuote"]:
        sym = item.get("symbol", "")
        if sym not in name_map or not item.get("last"):
            continue
        try:
            # CNBC 返回带百分号的字符串（如 "4.913%"），需剥离
            out.append({"name": name_map[sym],
                        "yield": float(str(item["last"]).rstrip("%")),
                        "chg": float(str(item.get("change") or 0).rstrip("%"))})
        except (TypeError, ValueError):
            continue
    if not out:
        raise RuntimeError("CNBC 未返回有效收益率数据")
    return out


def fetch_fred_curve() -> dict:
    """FRED 美债收益率曲线当日各期限。失败返回空 dict。"""
    try:
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2,DGS10,DGS30&cosd=2026-10-20"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        lines = urllib.request.urlopen(req, timeout=25).read().decode().strip().splitlines()
        last = lines[-1].split(",")
        vals = {}
        for tenor, v in zip(("2年", "10年", "30年"), last[1:]):
            try:
                if v and float(v) > 0:
                    vals[tenor] = float(v)
            except ValueError:
                continue
        return vals
    except Exception as e:
        news.log(f"FRED 曲线获取失败（跳过）: {e}")
        return {}


def ai_bonds_comment(yields: list, headlines: list) -> list:
    yline = "；".join(f"{y['name']}10年期 {y['yield']:.2f}%（日变动 {y['chg']:+.3f}）"
                      for y in yields)
    prompt = (
        "你是利率债分析师，为报纸全球国债版写「债市点评」。基于以下各国10年期国债收益率"
        "与财经素材，写 4-5 条要点（挑变动有信息量的国家，每条≤45字：收益率变动的"
        "政策/通胀/供给逻辑与关注点）+ 最后一条风险提示（≤40字）。每条以 - 开头。"
        "直接输出 JSON {\"comment\": [...]}。变动微小处注明是判断。\n\n"
        f"收益率：{yline}\n相关财经要闻：" + "；".join(headlines[:15])
    )
    try:
        text = news.llm_complete([{"role": "user", "content": prompt}],
                                 max_tokens=700, temperature=0.35)
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return [str(x) for x in data.get("comment", [])]
    except Exception as e:
        news.log(f"债市点评生成失败（跳过）: {e}")
        return []


def render_bonds_page(now, yields, curve, comment):
    plt = _setup()
    pg = Page(plt)
    pg.masthead(now, "全球国债版")

    ys = sorted(yields, key=lambda x: x["yield"])
    ax1 = pg.fig.add_axes([0.07, 0.675, 0.86, 0.225])
    ax1.barh([y["name"] for y in ys], [y["yield"] for y in ys],
             color=ACC, height=0.58, alpha=0.85)
    for yv in ys:
        ax1.text(yv["yield"] + 0.05, ys.index(yv), f"{yv['yield']:.2f}%（{yv['chg']:+.3f}）",
                 va="center", fontsize=8.5, color="#333333")
    ax1.set_title("各国 10 年期国债收益率 %（括号内为日变动）", fontsize=12, pad=6)
    ax1.tick_params(labelsize=9)
    ax1.spines[["top", "right"]].set_visible(False)
    ax1.set_xlim(0, max(y["yield"] for y in ys) * 1.32)
    _style_chart_ax(ax1)

    if curve:
        ax2 = pg.fig.add_axes([0.07, 0.41, 0.40, 0.20])
        tenors = list(curve.keys())
        vals = [curve[t] for t in tenors]
        ax2.plot(tenors, vals, marker="o", color=ACC, lw=2)
        for t, v in zip(tenors, vals):
            ax2.text(t, v + 0.08, f"{v:.2f}%", ha="center", fontsize=9, color=INK)
        ax2.set_title("美债收益率曲线（当日）", fontsize=11.5, pad=6)
        ax2.grid(True, lw=0.4, alpha=0.4)
        ax2.tick_params(labelsize=9)
        ax2.set_ylim(min(vals) - 0.5, max(vals) + 0.5)
        _style_chart_ax(ax2)

    rows = [{"name": y["name"], "price": y["yield"], "pct": y["chg"]} for y in yields]
    table_x, table_w, table_y = (0.55, 0.40, 0.575) if curve else (0.05, 0.9, 0.60)
    draw_quote_table(pg, table_x, table_y, table_w, rows,
                     title="10年期收益率一览（日变动）", first_col="国家",
                     third_header="日变动", show_fourth=False)

    if comment:
        deco_top = 0.355 if curve else max(table_end_y() if False else 0.355, 0.10)
        _comment_box(pg, 0.05, deco_top if curve else 0.355, comment,
                     box_color="#f2ede2", edge="#c9b98a", head_color=ACC, title="债市点评")

    pg.rule(0.052, 0.6, "#999999")
    pg.ax.text(0.05, 0.036, "收益率来源：CNBC ｜ 曲线：FRED ｜ 点评：DeepSeek ｜ 日变动为绝对值",
               fontsize=8.5, color=SUB)
    pg.ax.text(0.95, 0.036, "利率有风险，配置需谨慎", fontsize=8.5, color=SUB, ha="right")
    return pg.save("newspaper_bonds.png")
