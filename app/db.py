"""数据层: SQLAlchemy 模型 + 会话工厂

实体:
- Account    采集账号(浏览器画像/代理/风控状态)
- Task       监控任务(关键词/创作者, 调度参数)
- Note       采集到的笔记(原始数据 + 内容指纹)
- Analysis   内容识别结果(规则/LLM)
- RiskEvent  风控事件流水(审计)
- AlertRecord 推送记录(防重发)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, Index,
    Integer, String, Text, UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def _now() -> datetime:
    return datetime.now()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    status = Column(String(16), default="active", nullable=False)
    # active / cooldown / needs_login / disabled
    proxy = Column(String(256), default="")
    risk_state = Column(JSON, default=dict)     # RiskEngine 的持久化状态
    last_used_at = Column(DateTime)
    created_at = Column(DateTime, default=_now)

    def __repr__(self):
        return f"<Account {self.name} status={self.status}>"


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    task_type = Column(String(16), default="keyword")  # keyword / creator
    keyword = Column(String(256), nullable=False)      # 关键词或创作者ID
    enabled = Column(Boolean, default=True)
    interval_minutes = Column(Integer, default=120)    # 采集周期
    max_notes_per_run = Column(Integer, default=20)    # 单次采集上限
    fetch_detail = Column(Boolean, default=False)      # 是否进详情页取正文
    last_run_at = Column(DateTime)
    next_run_at = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("note_id", name="uq_note_id"),
        Index("ix_notes_collected", "collected_at"),
        Index("ix_notes_task", "task_id"),
    )

    id = Column(Integer, primary_key=True)
    note_id = Column(String(64), nullable=False)
    xsec_token = Column(Text, default="")
    title = Column(Text, default="")
    desc = Column(Text, default="")
    note_type = Column(String(16), default="normal")   # normal / video
    user_id = Column(String(64), default="")
    user_nickname = Column(String(128), default="")
    liked_count = Column(String(32), default="")       # 平台原文(如 "1.2万")
    comment_count = Column(String(32), default="")
    liked_count_num = Column(Integer, default=0)       # 解析后的数值
    url = Column(Text, default="")
    media = Column(JSON, default=list)                 # 封面/图片列表
    content_hash = Column(String(32), default="")      # 归一化正文 MD5(精确去重)
    simhash = Column(String(16), default="")           # 64bit simhash hex(近似去重)
    task_id = Column(Integer)
    keyword = Column(String(256), default="")
    collected_at = Column(DateTime, default=_now)
    raw = Column(JSON, default=dict)                   # 原始 JSON(溯源)


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("note_id", name="uq_analysis_note"),)

    id = Column(Integer, primary_key=True)
    note_id = Column(String(64), nullable=False)
    matched_rules = Column(JSON, default=list)   # 命中的规则名
    relevance = Column(Float, default=0.0)       # 0-100 相关度
    category = Column(String(32), default="other")
    sentiment = Column(String(16), default="neutral")
    is_alert = Column(Boolean, default=False)    # 是否触发告警
    summary = Column(Text, default="")           # LLM 摘要(可选)
    analyzed_by = Column(String(8), default="rules")  # rules / llm
    created_at = Column(DateTime, default=_now)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    __table_args__ = (Index("ix_risk_events_time", "created_at"),)

    id = Column(Integer, primary_key=True)
    account_name = Column(String(64), nullable=False)
    event_type = Column(String(32), nullable=False)
    # risk_signal / cooldown_start / cooldown_end / probe_ok / probe_fail /
    # account_disabled / quota_exceeded
    level = Column(Integer, default=0)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


class AlertRecord(Base):
    __tablename__ = "alert_records"

    id = Column(Integer, primary_key=True)
    note_id = Column(String(64), nullable=False)
    channel = Column(String(32), nullable=False)
    ok = Column(Boolean, default=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime, default=_now)


class SysState(Base):
    """通用键值状态存储(监督层暂停/偏好账号/任务失败计数等)."""

    __tablename__ = "sys_state"

    key = Column(String(64), primary_key=True)
    value = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=_now)


class SupervisorLog(Base):
    """监督层决策审计流水."""

    __tablename__ = "supervisor_logs"
    __table_args__ = (Index("ix_supervisor_time", "created_at"),)

    id = Column(Integer, primary_key=True)
    rationale = Column(Text, default="")
    actions = Column(JSON, default=list)
    context = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_now)


class Database:
    """轻量封装: 引擎 + 会话工厂 + 初始化."""

    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, future=True)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False,
                                    future=True)

    def init(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self):
        return self.Session()

    def get_state(self, key: str, default=None):
        with self.session() as s:
            row = s.get(SysState, key)
            return row.value if row else default

    def set_state(self, key: str, value) -> None:
        with self.session() as s:
            row = s.get(SysState, key)
            if row:
                row.value = value
                row.updated_at = _now()
            else:
                s.add(SysState(key=key, value=value))
            s.commit()
