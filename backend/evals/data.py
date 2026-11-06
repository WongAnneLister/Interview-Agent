"""评测数据：模拟候选人人设（personas/*.yaml）和虚构的 JD + 简历（fixtures/*.yaml）。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from interview.db.models import InterviewStyle, InterviewType, Language

EVALS_DIR = Path(__file__).resolve().parent
PERSONAS_DIR = EVALS_DIR / "personas"
FIXTURES_DIR = EVALS_DIR / "fixtures"


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    behavior: str = Field(min_length=1)
    expected_score: tuple[float, float]
    answer_chars: tuple[int, int]
    think_ratio: float = Field(default=0.0, ge=0, le=1)  # 先说“我想一下”的题目比例
    think_pause_s: tuple[float, float] = (8.0, 15.0)
    style: InterviewStyle | None = None  # 覆盖 fixture 的面试风格（冗长人设用压力面）
    adversarial: bool = False

    @model_validator(mode="after")
    def check_ranges(self) -> Persona:
        for low, high in (self.expected_score, self.answer_chars, self.think_pause_s):
            if low > high:
                raise ValueError("区间下限不能大于上限")
        return self


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    type: InterviewType
    style: InterviewStyle
    language: Language
    jd: str = Field(min_length=1)
    resume: str = Field(min_length=1)


def _load[M: BaseModel](directory: Path, model: type[M]) -> dict[str, M]:
    items: dict[str, M] = {}
    for path in sorted(directory.glob("*.yaml")):
        item = model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        item_id = str(getattr(item, "id"))  # noqa: B009
        if item_id != path.stem:
            raise ValueError(f"{path.name}：id {item_id!r} 必须与文件名一致")
        items[item_id] = item
    return items


def load_personas(directory: Path = PERSONAS_DIR) -> dict[str, Persona]:
    return _load(directory, Persona)


def load_fixtures(directory: Path = FIXTURES_DIR) -> dict[str, Fixture]:
    return _load(directory, Fixture)


def select(available: dict[str, object], wanted: list[str] | None, kind: str) -> list[str]:
    """按命令行参数挑选；未指定时全选。未知 id 直接报错，避免拼错后静默少跑。"""
    if not wanted:
        return list(available)
    unknown = [item for item in wanted if item not in available]
    if unknown:
        raise ValueError(f"未知的{kind}：{', '.join(unknown)}（可选：{', '.join(available)}）")
    return wanted
