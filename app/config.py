"""配置管理: YAML 主配置 + 环境变量敏感项覆盖"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class BehaviorConfig:
    """拟人行为参数 — 反风控策略的"行为真实性"维度.

    第一性原理: 平台风控本质是对流量分布做信号检测(环境/行为/时序/网络四维)。
    Agent 策略 = 让自身流量分布尽量贴合真人分布, 同时总盘子足够小。
    """

    # 相邻两次请求的基础间隔(秒), 实际取 [min, max] 均匀分布
    request_interval_min: float = 90.0
    request_interval_max: float = 180.0
    page_load_timeout: int = 30

    # 滚动阅读: 轮数与每轮滚动像素
    scroll_rounds_min: int = 2
    scroll_rounds_max: int = 5
    scroll_dy_min: int = 350
    scroll_dy_max: int = 900
    reverse_scroll_prob: float = 0.15  # 偶尔回滚(真人会回看)

    # 停留时长: 与内容长度正相关(真人的阅读时间 ~ 字数)
    dwell_ms_per_char: float = 18.0
    dwell_ms_min: float = 1500.0
    dwell_ms_max: float = 12000.0

    # 打字: 逐字符延迟(搜索框输入)
    type_delay_ms_min: int = 60
    type_delay_ms_max: int = 190


@dataclass
class RiskConfig:
    """风控引擎参数 — 反风控策略的"节奏经济学"维度.

    对抗性原则: 宁可慢, 不可封。所有配额都是保护线, 0 表示沿用默认保护线。
    """

    hourly_search_quota: int = 8          # 单账号每小时搜索次数上限
    daily_search_quota: int = 50          # 单账号每天搜索次数上限
    hourly_detail_quota: int = 25         # 单账号每小时笔记详情数上限
    daily_detail_quota: int = 150         # 单账号每天笔记详情数上限

    # 冷却阶梯(分钟): 风险等级 0..5 对应的冷却时长
    cooldown_ladder_minutes: list = field(
        default_factory=lambda: [0, 5, 30, 120, 720, 1440]
    )
    recover_success_needed: int = 3       # 冷却结束后连续成功 N 次才降一级(渐进恢复)
    min_request_gap_seconds: float = 75.0  # 搜索类动作的硬性最小间隔
    min_detail_gap_seconds: float = 6.0    # 会话内翻看笔记详情的硬性间隔(真人翻页只需几秒)

    # 触发风控信号的 HTTP 状态码 / 业务码(小红书风控常见码)
    risk_http_codes: list = field(
        default_factory=lambda: [403, 429, 461, 471]
    )
    risk_biz_codes: list = field(
        default_factory=lambda: [461, 471, -100]
    )


@dataclass
class LLMConfig:
    """内容深度识别(可选): OpenAI 兼容接口, 支持 DeepSeek/Qwen/GLM 等"""

    enabled: bool = False
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key_env: str = "XHS_LLM_API_KEY"   # 从环境变量读密钥, 不落盘
    batch_size: int = 8
    timeout_seconds: int = 60
    temperature: float = 0.1


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass
class SchedulerConfig:
    tick_seconds: int = 30
    # 任务启动抖动(秒): 故意错开整点节奏, 避免周期性指纹(对抗性原则)
    start_jitter_max_seconds: int = 180
    # 采集活跃时段(本地时间), 之外不采集 — 贴合真人作息
    active_hours_start: int = 7
    active_hours_end: int = 23
    # 任务间隔的随机浮动范围 ±15%
    interval_jitter_ratio: float = 0.15


@dataclass
class NotifyConfig:
    feishu_webhooks: list = field(default_factory=list)
    dingtalk_webhooks: list = field(default_factory=list)
    wecom_webhooks: list = field(default_factory=list)
    generic_webhooks: list = field(default_factory=list)
    # 邮件(SMTP)
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password_env: str = "XHS_SMTP_PASSWORD"
    smtp_to: list = field(default_factory=list)
    # 摘要节流: 两次推送的最小间隔(分钟), 0 不节流
    digest_throttle_minutes: int = 10
    # 高优先级(负面舆情)是否绕过节流
    critical_bypass_throttle: bool = True


@dataclass
class SupervisorConfig:
    """监督决策层(LLM 兜底策略)参数."""

    enabled: bool = True
    min_activation_interval_minutes: int = 5   # 两次 LLM 决策最小间隔
    risk_event_window_minutes: int = 30        # 风控信号统计窗口
    risk_event_threshold: int = 3              # 窗口内信号数达到即激活
    task_failure_threshold: int = 3            # 任务连续失败达到即激活
    max_interval_minutes: int = 1440           # scale_intervals 的周期上限


@dataclass
class AccountConfig:
    name: str
    proxy: Optional[str] = None       # http://user:pass@host:port — 一号一出口, 长期稳定
    viewport: Optional[list] = None   # 每账号固定窗口尺寸, 避免画像漂移


@dataclass
class AppConfig:
    db_url: str = "sqlite:///./data/monitor.db"
    profiles_dir: str = "./data/profiles"
    keywords_file: str = "./keywords.yaml"
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    web: WebConfig = field(default_factory=WebConfig)
    accounts: list = field(default_factory=list)  # list[dict]

    def account_configs(self) -> list[AccountConfig]:
        return [
            AccountConfig(
                name=a["name"],
                proxy=a.get("proxy"),
                viewport=a.get("viewport"),
            )
            for a in (self.accounts or [])
        ]


def load_config(path: str | Path = "./config.yaml") -> AppConfig:
    """加载 YAML 配置; 不存在时返回默认值(便于首次 init)."""
    path = Path(path)
    data = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    def _sub(cls, d):
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    cfg = AppConfig(
        db_url=data.get("db_url", AppConfig.db_url),
        profiles_dir=data.get("profiles_dir", AppConfig.profiles_dir),
        keywords_file=data.get("keywords_file", AppConfig.keywords_file),
        behavior=_sub(BehaviorConfig, data.get("behavior")),
        risk=_sub(RiskConfig, data.get("risk")),
        llm=_sub(LLMConfig, data.get("llm")),
        scheduler=_sub(SchedulerConfig, data.get("scheduler")),
        notify=_sub(NotifyConfig, data.get("notify")),
        supervisor=_sub(SupervisorConfig, data.get("supervisor")),
        web=_sub(WebConfig, data.get("web")),
        accounts=data.get("accounts") or [],
    )
    # 敏感项允许环境变量覆盖
    if os.environ.get("XHS_DB_URL"):
        cfg.db_url = os.environ["XHS_DB_URL"]
    return cfg
