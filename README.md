# 飞书财经早报机器人

每天早上 8:00（北京时间）自动抓取财经新闻，分类后以卡片消息推送到飞书群。纯 Python 标准库实现，无第三方依赖。

**当前主用：GitHub Actions 云端推送**（`.github/workflows/daily.yml`）——定时任务跑在 GitHub 服务器上，与本机开关机无关，手机/任何设备登录飞书都能收到。密钥通过仓库 Secrets（`FEISHU_WEBHOOK_URL`、`FEISHU_SECRET`）注入，不入库。

也可切换回本地模式：Windows 计划任务 `FeishuNewsBot`（见下文「注册每日定时任务」），密钥放在本地 `config.json`。

## 推送内容

- 📈 A股与国内宏观
- 🌍 全球市场与美股（含 WSJ 英文要闻）
- 🏢 行业与公司动态
- 📌 其他要闻（兜底）

数据源：华尔街见闻 RSS（主源）+ WSJ Markets（补充）。自动按关键词分类、按发布时间过滤（默认近 24 小时 + 上次推送以来的新消息），链接级去重（state.json 记录最近 500 条，云端模式由 Actions 每日回写提交）。

## 云端运维（GitHub Actions）

| 操作 | 说明 |
|---|---|
| 手动触发 | 仓库 Actions 页 → daily-finance-news → Run workflow，可选「只发测试卡片」 |
| 看运行记录/日志 | 同上页面，每次推送一条记录，失败会标红 |
| 改推送时间 | 编辑 `daily.yml` 里的 `cron`（UTC 时区，北京时间 -8 小时） |
| 改密钥 | 仓库 Settings → Secrets and variables → Actions |

注意：GitHub 定时任务可能延迟几分钟到几十分钟（免费版特性），所以 cron 设在 07:56 而非 08:00；仓库长期（60 天）无任何提交时 GitHub 会自动暂停定时工作流，去重状态的每日回写通常足以避免，若仍被暂停到 Actions 页面点 Enable 即可。

## 本地模式（备用）

1. 复制 `config.example.json` 为 `config.json`，填入 Webhook 地址（若开启签名校验再填密钥）
2. 验证连通：`python feishu_news.py --test`，群里收到绿色测试卡片即成功

### 注册每日定时任务（管理员不需要）

```powershell
powershell -ExecutionPolicy Bypass -File register_task.ps1
```

每天 08:00 自动运行；错过时刻（关机/睡眠）会在下次开机可用时补推。

## 日常运维

| 操作 | 命令 |
|---|---|
| 手动立刻推送一次 | `python feishu_news.py` |
| 预览内容不发送 | `python feishu_news.py --dry-run` |
| 看运行日志 | `logs/run.log` |
| 删除定时任务 | `schtasks /Delete /TN FeishuNewsBot /F` |

调整 `config.json` 可改：每板块条数（`max_per_category`）、回看时间窗（`hours_lookback`）、数据源列表。调整推送时间用「任务计划程序」改 FeishuNewsBot 的触发器即可。

## 文件说明

- `feishu_news.py` — 主脚本（抓取/分类/卡片/签名/重试）
- `config.json` — Webhook、密钥、条数、数据源
- `state.json` — 去重状态（自动生成，可随时删除）
- `register_task.ps1` — 注册 Windows 计划任务
