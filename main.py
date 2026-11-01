"""小红书内容监控 Agent — 主入口

用法:
  python main.py init                    # 初始化数据库 + 默认配置/关键词
  python main.py login --name acc1       # 弹出浏览器扫码登录账号
  python main.py add-task --name 防尘口罩 --keyword "防尘口罩" [--interval 120]
  python main.py run [--task-id N]       # 跑一轮采集(不等调度)
  python main.py start                   # 常驻: 调度器 + Web 控制台
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.browser import BrowserManager
from app.classifier import RuleClassifier
from app.config import AppConfig, load_config
from app.db import Account, Database, Task
from app.llm import LLMAnalyzer
from app.notifier import Notifier
from app.pipeline import MonitorPipeline
from app.risk import RiskEngine
from app.scheduler import Scheduler
from app.supervisor import SupervisorAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("main")

DEFAULT_CONFIG = """\
# 小红书内容监控 Agent 配置
db_url: sqlite:///./data/monitor.db
profiles_dir: ./data/profiles
keywords_file: ./keywords.yaml

accounts:
  - name: acc1
    proxy: ""              # http://user:pass@host:port; 一号一出口, 长期固定
    viewport: [1280, 800]  # 每账号固定窗口, 避免画像漂移

behavior:                  # 拟人行为(详见 app/behavior.py)
  request_interval_min: 90
  request_interval_max: 180
  page_load_timeout: 60
  scroll_rounds_min: 2
  scroll_rounds_max: 5
  dwell_ms_per_char: 18
  dwell_ms_max: 12000

risk:                      # 风控引擎(详见 app/risk.py)
  hourly_search_quota: 8
  daily_search_quota: 50
  hourly_detail_quota: 25
  daily_detail_quota: 150
  cooldown_ladder_minutes: [0, 5, 30, 120, 720, 1440]
  recover_success_needed: 3
  min_request_gap_seconds: 75
  min_detail_gap_seconds: 6

scheduler:
  tick_seconds: 30
  start_jitter_max_seconds: 180
  active_hours_start: 7
  active_hours_end: 23
  interval_jitter_ratio: 0.15

supervisor:                # 监督决策层: LLM 兜底策略(平时静默, 异常时激活)
  enabled: true
  min_activation_interval_minutes: 5
  risk_event_window_minutes: 30
  risk_event_threshold: 3
  task_failure_threshold: 3
  max_interval_minutes: 1440

llm:                       # 可选: OpenAI 兼容接口(DeepSeek/Qwen/GLM)
  enabled: false
  base_url: https://api.deepseek.com/v1
  model: deepseek-chat
  api_key_env: XHS_LLM_API_KEY

notify:
  feishu_webhooks: []      # https://open.feishu.cn/open-apis/bot/v2/hook/xxx
  dingtalk_webhooks: []
  wecom_webhooks: []
  generic_webhooks: []
  smtp_host: ""
  smtp_to: []
  digest_throttle_minutes: 10

web:
  enabled: true
  host: 127.0.0.1
  port: 8787
"""

DEFAULT_KEYWORDS = """\
# 内容识别规则(规则引擎 L1); LLM 开启后自动语义增强
base_keywords: [防尘口罩, 呼吸防护, KN95, N95, 口罩, 护目镜, 耳塞]

rules:
  - name: brand_complaint      # 品牌负面舆情(高优先级告警)
    include: [过敏, 假货, 投诉, 维权, 退货, 退款, 智商税]
    category: complaint
    sentiment: negative
    alert: true
    weight: 20

  - name: product_praise       # 正面口碑
    include: [好用, 回购, 推荐, 透气, 舒服, 性价比]
    category: brand_mention
    sentiment: positive
    weight: 10

  - name: competitor_watch     # 竞品动态
    include: [3M, 霍尼韦尔, 代尔塔, 优维斯, 思创, 稳健]
    category: competitor
    weight: 12

  - name: industry_trend       # 行业趋势/政策
    include: [新国标, GB2626, 强制标准, 招标, 采购, 集采]
    category: industry_trend
    weight: 15

  - name: shopping_intent      # 购买意向(电商运营价值)
    include: [求推荐, 哪个牌子, 怎么选, 有没有, 蹲一个, 求链接]
    category: shopping_intent
    weight: 12
"""


def build_stack(cfg: AppConfig, db: Database):
    """组装整个 Agent 的依赖栈."""
    import yaml
    classifier = RuleClassifier(rules_cfg=[], base_keywords=[])
    kw_path = Path(cfg.keywords_file)
    if kw_path.exists():
        with open(kw_path, "r", encoding="utf-8") as f:
            kw = yaml.safe_load(f) or {}
        classifier = RuleClassifier(
            rules_cfg=kw.get("rules") or [],
            base_keywords=kw.get("base_keywords") or [])
    else:
        log.warning("关键词文件 %s 不存在, 规则引擎为空", kw_path)

    llm = LLMAnalyzer(cfg.llm)
    notifier = Notifier(cfg.notify, db)
    risk = RiskEngine(cfg.risk, db)
    browsers = BrowserManager(cfg)
    pipeline = MonitorPipeline(cfg, db, classifier, llm, notifier,
                               risk, browsers)
    supervisor = SupervisorAgent(cfg, db, llm, notifier, risk)
    return classifier, llm, notifier, risk, browsers, pipeline, supervisor


async def cmd_init(cfg: AppConfig, db: Database) -> None:
    db.init()
    Path(cfg.profiles_dir).mkdir(parents=True, exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    if not Path("config.yaml").exists():
        Path("config.yaml").write_text(DEFAULT_CONFIG, encoding="utf-8")
        print("已生成 config.yaml")
    if not Path("keywords.yaml").exists():
        Path("keywords.yaml").write_text(DEFAULT_KEYWORDS, encoding="utf-8")
        print("已生成 keywords.yaml")
    # 配置文件可能刚生成(或用户刚编辑过), 重新加载后再登记账号
    cfg = load_config("config.yaml")
    with db.session() as sess:
        for ac in cfg.account_configs():
            exists = sess.query(Account).filter_by(name=ac.name).first()
            if not exists:
                sess.add(Account(name=ac.name, proxy=ac.proxy or ""))
                print(f"账号 [{ac.name}] 已登记")
        sess.commit()
    print("初始化完成: data/monitor.db")


async def cmd_login(cfg: AppConfig, db: Database, name: str) -> None:
    db.init()
    browsers = BrowserManager(cfg)
    with db.session() as sess:
        account = sess.query(Account).filter_by(name=name).first()
        if account is None:
            print(f"账号 [{name}] 不存在, 请先 init / 修改 config.yaml")
            return
    ok = await browsers.interactive_login(account)
    sys.exit(0 if ok else 1)


async def cmd_add_task(cfg, db, name, keyword, interval, max_notes,
                       fetch_detail) -> None:
    db.init()
    with db.session() as sess:
        t = Task(name=name, keyword=keyword, interval_minutes=interval,
                 max_notes_per_run=max_notes, fetch_detail=fetch_detail,
                 next_run_at=datetime.now())
        sess.add(t)
        sess.commit()
        print(f"任务已创建: #{t.id} [{name}] keyword={keyword} "
              f"interval={interval}min")


async def cmd_run(cfg, db, task_id: int | None) -> None:
    db.init()
    *_, risk, browsers, pipeline, supervisor = build_stack(cfg, db)
    try:
        report = await pipeline.run_cycle(
            task_ids=[task_id] if task_id else None)
        print(f"\n{report.summary()}")
        if report.errors:
            print("错误:", *report.errors, sep="\n  - ")
        if report.risk_blocked:
            print("风控拦截:", *report.risk_blocked, sep="\n  - ")
    finally:
        await browsers.close_all()


async def cmd_start(cfg: AppConfig, db: Database) -> None:
    db.init()
    *_, risk, browsers, pipeline, supervisor = build_stack(cfg, db)
    scheduler = Scheduler(cfg, db, pipeline, supervisor=supervisor)

    servers = []
    if cfg.web.enabled:
        import uvicorn
        from app.web import create_app
        app = create_app(cfg, db, risk, pipeline, supervisor=supervisor)
        uconfig = uvicorn.Config(app, host=cfg.web.host, port=cfg.web.port,
                                 log_level="warning")
        server = uvicorn.Server(uconfig)
        servers.append(asyncio.ensure_future(server.serve()))
        print(f"Web 控制台: http://{cfg.web.host}:{cfg.web.port}")

    sched_task = asyncio.ensure_future(scheduler.run_forever())
    servers.append(sched_task)

    print("Agent 已启动 (Ctrl+C 退出)")
    try:
        await asyncio.gather(*servers)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.stop()
        await browsers.close_all()
        print("已安全退出")


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书内容监控 Agent")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    p_login = sub.add_parser("login")
    p_login.add_argument("--name", required=True)
    p_task = sub.add_parser("add-task")
    p_task.add_argument("--name", required=True)
    p_task.add_argument("--keyword", required=True)
    p_task.add_argument("--interval", type=int, default=120)
    p_task.add_argument("--max-notes", type=int, default=20)
    p_task.add_argument("--detail", action="store_true")
    p_run = sub.add_parser("run")
    p_run.add_argument("--task-id", type=int, default=None)
    sub.add_parser("start")

    args = parser.parse_args()
    cfg = load_config(args.config)
    db = Database(cfg.db_url)

    if args.cmd == "init":
        asyncio.run(cmd_init(cfg, db))
    elif args.cmd == "login":
        asyncio.run(cmd_login(cfg, db, args.name))
    elif args.cmd == "add-task":
        asyncio.run(cmd_add_task(cfg, db, args.name, args.keyword,
                                 args.interval, args.max_notes,
                                 args.detail))
    elif args.cmd == "run":
        asyncio.run(cmd_run(cfg, db, args.task_id))
    elif args.cmd == "start":
        try:
            asyncio.run(cmd_start(cfg, db))
        except KeyboardInterrupt:
            print("\n已退出")


if __name__ == "__main__":
    main()
