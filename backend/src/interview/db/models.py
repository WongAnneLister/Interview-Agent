"""业务表的 ORM 模型。

约定（改动前先读）：
- 主键 UUID（uuid4，应用端生成，数据库端也有默认值）；时间一律 timestamptz。
- 除 users 外每张表都有 user_id，并继承 UserOwned 作为标记，查询必须按 user_id 过滤。
- 面试的子表用复合外键 (interview_id, user_id) → interviews(id, user_id)，
  由数据库保证子表的 user_id 与所属面试一致；删除面试时子表级联删除。
- 枚举存为 varchar + CHECK 约束（不用 PostgreSQL 原生枚举），增删取值只需改约束。
- question_id、competency 指向 plans 里 JSON 中的 id，不建外键。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import SmallInteger

# 约束名固定可预测，Alembic 迁移里的名字不依赖数据库自动命名
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


class UserOwned:
    """标记：带 user_id 的业务表。仓储层和会话拦截按这个标记强制按 user_id 过滤。"""

    user_id: Mapped[uuid.UUID]


class InterviewType(enum.StrEnum):
    TECHNICAL = "technical"
    BEHAVIORAL = "behavioral"


class InterviewStyle(enum.StrEnum):
    GENTLE = "gentle"
    PRESSURE = "pressure"


class Language(enum.StrEnum):
    ZH = "zh"
    EN = "en"


class InterviewStatus(enum.StrEnum):
    DRAFT = "draft"  # 已创建，计划未生成
    PLANNED = "planned"  # 计划已生成，等待开始
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"  # 正常走完
    ABORTED = "aborted"  # 候选人提前结束或断线
    FAILED = "failed"  # 系统错误中断


class TurnRole(enum.StrEnum):
    INTERVIEWER = "interviewer"
    CANDIDATE = "candidate"


class NextAction(enum.StrEnum):
    FOLLOW_UP = "follow_up"  # 追问
    NEXT_QUESTION = "next_question"  # 换题
    NEXT_STAGE = "next_stage"  # 换阶段


class EvaluationPhase(enum.StrEnum):
    LIVE = "live"  # 面试中的实时评分，只用于决定追问
    FINAL = "final"  # 面试结束后基于全部转写重新评定，进报告


class ReportStatus(enum.StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


def _enum_values(cls: type[enum.Enum]) -> list[str]:
    return [str(m.value) for m in cls]


def _enum(cls: type[enum.StrEnum], name: str) -> Enum:
    """varchar + CHECK 约束；存枚举的值（小写字符串）而不是成员名。"""
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=_enum_values,
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now())


def _owner_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("users.id", ondelete="CASCADE"))


def _interview_fk() -> ForeignKeyConstraint:
    """子表 → interviews 的复合外键，保证 user_id 与所属面试一致。"""
    return ForeignKeyConstraint(
        ["interview_id", "user_id"], ["interviews.id", "interviews.user_id"], ondelete="CASCADE"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created_at()


# 邮箱不区分大小写唯一
Index("uq_users_lower_email", func.lower(User.email), unique=True)


class Resume(UserOwned, Base):
    __tablename__ = "resumes"
    __table_args__ = (Index("ix_resumes_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _owner_fk()
    file_key: Mapped[str] = mapped_column(Text)  # 对象存储里的 key，不存 URL
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 解析前为空
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Interview(UserOwned, Base):
    __tablename__ = "interviews"
    __table_args__ = (
        UniqueConstraint("id", "user_id"),  # 子表复合外键的引用目标
        CheckConstraint("duration_min IN (15, 20, 30)", name="duration_min"),
        Index("ix_interviews_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _owner_fk()
    # 默认 NO ACTION：被面试引用的简历不能单独删除；删除用户时随用户一起级联删除
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id"), index=True)
    jd_text: Mapped[str] = mapped_column(Text)
    type: Mapped[InterviewType] = mapped_column(_enum(InterviewType, "interview_type"))
    style: Mapped[InterviewStyle] = mapped_column(_enum(InterviewStyle, "interview_style"))
    language: Mapped[Language] = mapped_column(_enum(Language, "language"))
    duration_min: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[InterviewStatus] = mapped_column(
        _enum(InterviewStatus, "interview_status"), default=InterviewStatus.DRAFT
    )
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Plan(UserOwned, Base):
    """面试计划，与面试一对一；用户修改计划时原地更新。"""

    __tablename__ = "plans"
    __table_args__ = (_interview_fk(),)

    interview_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column()
    competencies_json: Mapped[list[Any]] = mapped_column(JSONB)
    questions_json: Mapped[list[Any]] = mapped_column(JSONB)
    prompt_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Turn(UserOwned, Base):
    """一轮发言。start_ms / end_ms 是相对面试开始的毫秒数；被打断时 text 只含实际播出部分。"""

    __tablename__ = "turns"
    __table_args__ = (
        _interview_fk(),
        UniqueConstraint("interview_id", "seq"),
        CheckConstraint("end_ms >= start_ms", name="time_order"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column()
    user_id: Mapped[uuid.UUID] = mapped_column()
    seq: Mapped[int]
    role: Mapped[TurnRole] = mapped_column(_enum(TurnRole, "turn_role"))
    text: Mapped[str] = mapped_column(Text)
    start_ms: Mapped[int]
    end_ms: Mapped[int]
    interrupted: Mapped[bool] = mapped_column(default=False, server_default="false")
    question_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


class Evaluation(UserOwned, Base):
    """评估记录。score 为空表示“未考察到”；只有 live 评估有 next_action。"""

    __tablename__ = "evaluations"
    __table_args__ = (
        _interview_fk(),
        CheckConstraint("score BETWEEN 1 AND 5", name="score_range"),
        Index("ix_evaluations_interview_id_question_id", "interview_id", "question_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column()
    user_id: Mapped[uuid.UUID] = mapped_column()
    phase: Mapped[EvaluationPhase] = mapped_column(_enum(EvaluationPhase, "evaluation_phase"))
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="SET NULL"), index=True
    )
    question_id: Mapped[str] = mapped_column(String(64))
    competency: Mapped[str] = mapped_column(String(64))
    score: Mapped[int | None] = mapped_column(SmallInteger)
    evidence_json: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    next_action: Mapped[NextAction | None] = mapped_column(_enum(NextAction, "next_action"))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _created_at()


class Report(UserOwned, Base):
    """面试报告，与面试一对一；生成完成前 summary_json 和 generated_at 为空。"""

    __tablename__ = "reports"
    __table_args__ = (_interview_fk(),)

    interview_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[ReportStatus] = mapped_column(
        _enum(ReportStatus, "report_status"), default=ReportStatus.PENDING
    )
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    generated_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TurnMetric(UserOwned, Base):
    """每轮的语音时序指标（毫秒），缺失的环节为空。"""

    __tablename__ = "turn_metrics"
    __table_args__ = (_interview_fk(),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    interview_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID] = mapped_column()
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE"), unique=True
    )
    eou_ms: Mapped[int | None]
    stt_ms: Mapped[int | None]
    llm_ttft_ms: Mapped[int | None]
    tts_ttfb_ms: Mapped[int | None]
    e2e_ms: Mapped[int | None]
    barge_in: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = _created_at()
