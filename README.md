# 小红书内容监控 Agent (XHS Monitor Agent)

企业级小红书公开内容监控 Agent：定时采集公开笔记 → 内容识别（规则 + 可选 LLM）→ 风控自护 → 多渠道告警 → Web 控制台。

> 仅采集**公开内容**、只读不写（不点赞/不评论/不关注），用于品牌舆情与行业趋势监控。请遵守平台条款与当地法律法规，控制采集规模与频率。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                      调度器 scheduler                    │
│   反周期抖动启动 / 活跃时段约束(7-23点) / tick 30s          │
└──────────────────────────┬──────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────┐
│                   流水线 pipeline                        │
│  选号(等级最低/最久未用) → 采集 → 去重 → 识别 → 告警        │
└──┬──────────┬──────────┬──────────┬──────────┬──────────┘
   ▼          ▼          ▼          ▼          ▼
 浏览器层    风控引擎    去重器     识别层      推送层
 browser     risk       dedup    classifier  notifier
 一号一画像  配额/冷却   MD5+     规则L1+     飞书/钉钉/
 一号一出口  阶梯/探针   SimHash  LLM L2      企微/邮件
 持久Profile 渐进恢复             可降级      节流降噪
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
patchright install chromium   # 或 playwright install chromium

# 2. 初始化(生成 config.yaml / keywords.yaml / SQLite)
python main.py init

# 3. 扫码登录(弹出浏览器, 人工扫码, 仅此一次)
python main.py login --name acc1

# 4. 添加监控任务
python main.py add-task --name 防尘口罩舆情 --keyword "防尘口罩" --interval 120

# 5a. 立即跑一轮(测试)
python main.py run

# 5b. 常驻运行(调度器 + Web 控制台 http://127.0.0.1:8787)
python main.py start
```

## 反风控策略（Agent 层）

基于第一性原理：平台风控本质是对流量分布做信号检测，Agent 的目标不是“骗过风控”（军备竞赛必败），而是让自己落在风控**不值得动手**的区间。

| 维度 | 机制 | 代码位置 |
|---|---|---|
| **体积经济学** | 单账号读写配额压到真人日常量级（默认 8 搜索/小时、50/天） | `app/risk.py` |
| **信号响应** | 403/429/461/471/验证码 → 立即阶梯冷却（5min→30min→2h→12h→24h），绝不硬闯 | `app/risk.py` |
| **渐进恢复** | 冷却结束先进探针模式（单次最小动作），连续成功 3 次才降一级；恢复期间隔×(1+等级×0.5)、配额÷(等级+1) | `app/risk.py` |
| **行为真实性** | 间隔/滚动/停留/打字全部有界随机；停留时长与内容字数正相关；偶发回滚 | `app/behavior.py` |
| **反周期性** | 任务启动抖动 0-180s；下轮间隔 ±15% 浮动，永不周期 | `app/scheduler.py` |
| **环境一致性** | 一号一持久化 Profile、一号一固定代理出口（代理失效即停，绝不静默回落直连）；固定 viewport；有头浏览器 | `app/browser.py` |
| **最小暴露面** | 只读采集（无写操作）；登录仅人工扫码一次；patchright 反检测内核（无则回退 playwright） | `app/browser.py` `app/collector.py` |
| **协议克制** | 不逆向 x-s/x-t 签名，走真实页面发请求 + 网络层收割 JSON（借鉴 MediaCrawler） | `app/collector.py` |

## 内容识别

- **L1 规则引擎**（零成本，本地）：关键词/正则/情感词典/相关度打分，规则在 `keywords.yaml` 维护
- **L2 LLM 增强**（可选）：仅对疑似相关笔记批量语义分析（OpenAI 兼容接口，支持 DeepSeek/Qwen/GLM），失败自动降级回规则结果
- **两级去重**：note_id 精确去重 + SimHash 近似去重（海明距离≤3，识别矩阵账号洗稿刷屏）

## 告警

飞书 / 钉钉 / 企业微信 / 通用 Webhook / 邮件（SMTP）。摘要节流降噪（默认 10 分钟），负面舆情可绕过节流立即送达。推送记录落库可审计。

## Web 控制台

`python main.py start` 后访问 http://127.0.0.1:8787 ：账号风控状态、配额余量、任务管理（启停/立即运行）、最新采集（分类/情感/相关度过滤）、风控事件审计流水、账号人工重置。

## 目录结构

```
app/
  config.py     # 配置(YAML + 环境变量覆盖敏感项)
  db.py         # SQLAlchemy 模型(6 张表, 全审计可溯源)
  behavior.py   # 拟人行为模拟
  risk.py       # 风控引擎(配额/冷却阶梯/探针/渐进恢复)
  browser.py    # 浏览器会话管理(账号画像隔离)
  collector.py  # 页面拦截式采集器(不碰签名)
  dedup.py      # MD5 + SimHash 去重
  classifier.py # 规则引擎识别
  llm.py        # LLM 深度分析(可选, 可降级)
  notifier.py   # 多渠道推送
  pipeline.py   # 流水线编排
  scheduler.py  # 反周期调度
  web.py        # FastAPI 控制台
webui/index.html  # 控制台前端(零构建单文件)
main.py           # CLI 入口
```

## 致谢

架构借鉴了这些优秀开源项目：
- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)（64.7k★）— CDP + 页面拦截采集思路
- [TrendRadar](https://github.com/sansan0/TrendRadar)（62.1k★）— 调度/AI 流水线/降级策略/多渠道推送
- [creatorhub](https://github.com/3441293738/creatorhub)（1.9k★）— 风控中心/冷却阶梯/渐进恢复设计
- [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)（4.5k★）— 反检测浏览器内核
