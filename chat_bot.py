# -*- coding: utf-8 -*-
"""飞书财经助手·对话机器人（自建应用 + 长连接，跑在 GitHub Actions 上）

- 指令：早报（立即推一期新闻卡片）/ 帮助
- 其他文本：智谱 GLM Flash 免费模型回答（财经助手人设 + 近期要闻背景）
- 单聊直接回；群聊需 @机器人
- 续命：到达生命周期后通过 GitHub API 触发下一次运行再退出，实现 24/7 免费常驻
"""
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageRequest, ReplyMessageRequestBody

import feishu_news as news

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
ZHIPU_KEY = os.environ.get("ZHIPU_API_KEY", "")

LLM_MODELS = ["glm-4.7-flash", "glm-4.5-flash", "glm-4-flash-250414"]  # 依次回退
LIFETIME_MINUTES = int(os.environ.get("LIFETIME_MINUTES", "310"))  # 主动续命点，低于 job 硬超时
STABLE_MINUTES = 10  # 运行超过该时长才算"稳定过"，崩溃时才续命

BOT_OPEN_ID = None
HEADLINES = []
HISTORY = {}  # chat_id -> [{"role","content"}]
HISTORY_LIMIT = 16
HISTORY_LOCK = threading.Lock()
START_TS = time.time()

HELP_TEXT = (
    "📖 我是财经助手小财，可以这样用我：\n\n"
    "💬 直接发消息 —— 聊财经问题，AI 回答\n"
    "🌅 发「早报」—— 立即推送一期最新财经早报\n"
    "❓ 发「帮助」—— 显示这段说明\n\n"
    "群聊里需要 @我 才会回应，单聊直接说就行。"
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------- 飞书 API 基础 ----------

def get_tenant_token() -> str:
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data.get('msg')}")
    return data["tenant_access_token"]


def get_bot_open_id() -> str:
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {get_tenant_token()}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["bot"]["open_id"]
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"获取机器人信息失败: {last}")


def send_reply(api: lark.Client, message_id: str, msg_type: str, content) -> None:
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(ReplyMessageRequestBody.builder()
                      .msg_type(msg_type)
                      .content(json.dumps(content, ensure_ascii=False))
                      .build()) \
        .build()
    resp = api.im.v1.message.reply(req)
    if not resp.success():
        log(f"回复失败 code={resp.code} msg={resp.msg}")


# ---------- 智谱 GLM ----------

def zhipu_chat(messages: list) -> str:
    last_err = None
    for model in LLM_MODELS:
        for attempt in range(3):  # 免费档限流(429)时同模型退避重试
            try:
                body = {"model": model, "messages": messages, "temperature": 0.6, "max_tokens": 2048}
                req = urllib.request.Request(
                    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {ZHIPU_KEY}",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                answer = (data["choices"][0]["message"].get("content") or "").strip()
                if not answer:
                    answer = "（这次思考太久了没写出答案，再问一次试试）"
                return answer
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                last_err = f"HTTP {e.code}: {detail}"
                log(f"模型 {model} 失败(第{attempt + 1}次): {last_err}")
                if e.code == 401:
                    raise RuntimeError("智谱 API Key 无效（401），请检查 ZHIPU_API_KEY")
                if e.code == 429 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                break  # 其他错误换下一个模型
            except Exception as e:
                last_err = e
                log(f"模型 {model} 异常: {e}")
                break
    raise RuntimeError(f"所有模型均失败: {last_err}")


def system_prompt() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        "你是飞书里的财经助手「小财」，服务一位关注 A股、全球市场与行业动态的个人投资者。",
        "要求：中文回答；简洁直接、分点表述；给观点时说明依据；涉及行情预测时提示风险；",
        "不确定的数据不要编造，坦诚说明。聊天内容可能很随意，不必总往财经上靠。",
        f"今天是 {now.strftime('%Y年%m月%d日 %A')}。",
    ]
    if HEADLINES:
        lines.append("近期财经要闻标题（背景参考，非实时行情）：")
        lines.extend(f"- {t}" for t in HEADLINES)
    return "\n".join(lines)


# ---------- 消息处理 ----------

def fetch_headlines() -> None:
    global HEADLINES
    try:
        cfg = news.load_config(news.Path("config.json"))
        items = news.fetch_all(cfg["feeds"])
        seen, titles = set(), []
        for it in items:
            if it["title"] not in seen:
                seen.add(it["title"])
                titles.append(it["title"])
        HEADLINES = titles[:15]
        log(f"已加载 {len(HEADLINES)} 条要闻标题作为对话背景")
    except Exception as e:
        log(f"加载要闻失败（不影响对话）: {e}")


def send_digest(api: lark.Client, message_id: str) -> None:
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        cfg = news.load_config(news.Path("config.json"))
        items = news.fetch_all(cfg["feeds"])
        if not items:
            send_reply(api, message_id, "text", {"text": "新闻源暂时抓取失败，稍后再试~"})
            return
        card = news.build_card(items, cfg, now)
        send_reply(api, message_id, "interactive", card)
    except Exception as e:
        send_reply(api, message_id, "text", {"text": f"生成早报失败: {type(e).__name__}: {e}"})


def ai_reply(api: lark.Client, chat_id: str, message_id: str, text: str) -> None:
    if not ZHIPU_KEY:
        send_reply(api, message_id, "text", {
            "text": "AI 对话还没配置：需要在仓库 Secrets 里设置 ZHIPU_API_KEY。\n指令「早报」「帮助」目前可用。"})
        return
    with HISTORY_LOCK:
        history = HISTORY.setdefault(chat_id, [])
        history.append({"role": "user", "content": text})
        msgs = [{"role": "system", "content": system_prompt()}] + history[-HISTORY_LIMIT:]
    try:
        answer = zhipu_chat(msgs)
    except Exception as e:
        with HISTORY_LOCK:
            history.pop()
        send_reply(api, message_id, "text", {"text": f"AI 暂时没回答上来：{e}"})
        return
    with HISTORY_LOCK:
        history.append({"role": "assistant", "content": answer})
    send_reply(api, message_id, "text", {"text": answer})


def handle_message(data: P2ImMessageReceiveV1, api: lark.Client) -> None:
    try:
        msg = data.event.message
        if msg.message_type != "text":
            return
        try:
            content = json.loads(msg.content or "{}")
        except Exception:
            return
        text = content.get("text") or ""
        chat_id, message_id, chat_type = msg.chat_id, msg.message_id, msg.chat_type

        mentions = []
        if msg.mentions:
            for m in msg.mentions:
                # 事件里 Mention.id 是 UserId 对象（open_id 在内），兼容纯字符串形态
                mid = getattr(m, "id", None)
                oid = getattr(mid, "open_id", None) if not isinstance(mid, str) else mid
                if oid:
                    mentions.append(oid)
        if chat_type == "group":
            if BOT_OPEN_ID:
                if BOT_OPEN_ID not in mentions:
                    return
            elif not mentions:  # 拿不到机器人 ID 时退化为"有 @ 就回"
                return

        text = re.sub(r"<at[^>]*>.*?</at>", "", text).strip()
        if not text:
            return
        log(f"收到消息 chat={chat_id[:12]}… type={chat_type} text={text[:40]}")

        low = text.lower()
        if low in ("早报", "新闻", "今日早报", "推早报", "digest"):
            send_digest(api, message_id)
        elif low in ("帮助", "help", "?", "？", "指令", "菜单"):
            send_reply(api, message_id, "text", {"text": HELP_TEXT})
        else:
            ai_reply(api, chat_id, message_id, text)
    except Exception as e:
        log(f"处理消息异常: {type(e).__name__}: {e}")


# ---------- 续命与心跳 ----------

def trigger_next_run() -> None:
    """通过 GitHub API 触发下一次 chat-keepalive 运行，实现链式常驻。"""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("非 GitHub Actions 环境，跳过续命")
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/chat-keepalive.yml/dispatches",
        data=json.dumps({"ref": "main"}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()
    log("已触发下一次运行")


def watchdog() -> None:
    time.sleep(LIFETIME_MINUTES * 60)
    log("到达生命周期上限，续命并退出…")
    try:
        trigger_next_run()
    except Exception as e:
        log(f"续命失败（每日 03:00 的定时兜底会重新拉起）: {e}")
    os._exit(0)  # 跳过 finally，直接干净退出


def heartbeat() -> None:
    while True:
        time.sleep(1800)
        log(f"心跳正常 · 要闻背景 {len(HEADLINES)} 条 · 活跃会话 {len(HISTORY)}")


# ---------- 主流程 ----------

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not APP_ID or not APP_SECRET:
        log("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，无法启动。续命链已暂停，配置好后手动 Run workflow。")
        sys.exit(1)

    global BOT_OPEN_ID
    try:
        BOT_OPEN_ID = get_bot_open_id()
        log(f"机器人就绪，open_id={BOT_OPEN_ID}")
    except Exception as e:
        log(f"获取机器人 open_id 失败（群聊 @ 检测退化为宽松模式）: {e}")

    threading.Thread(target=fetch_headlines, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    api = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(lambda d: handle_message(d, api)) \
        .build()
    client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    log("对话机器人启动，长连接建立中…")
    try:
        client.start()
    except Exception as e:
        log(f"长连接异常退出: {type(e).__name__}: {e}")
        if time.time() - START_TS > STABLE_MINUTES * 60:
            try:
                trigger_next_run()
            except Exception as e2:
                log(f"续命失败: {e2}")
        sys.exit(1)


if __name__ == "__main__":
    main()
