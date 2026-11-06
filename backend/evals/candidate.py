"""模拟候选人：用大模型按人设回答面试官的话。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from evals.data import Persona
from interview.db.models import Language
from interview.prompts.loader import get_prompt_library
from interview.providers.base import LLMProvider

HISTORY_TURNS = 8  # 给模拟候选人看的最近几轮对话
THINK_PHRASE = {Language.ZH: "嗯……我想一下。", Language.EN: "Hmm, let me think for a moment."}


@dataclass
class SimulatedCandidate:
    llm: LLMProvider
    persona: Persona
    resume: str
    language: Language
    rng: random.Random
    history: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def wants_think(self) -> bool:
        """这一轮是否先说“我想一下”（紧张卡顿人设）。"""
        return self.rng.random() < self.persona.think_ratio

    def think_pause_ms(self) -> int:
        low, high = self.persona.think_pause_s
        return int(self.rng.uniform(low, high) * 1000)

    async def answer(self, interviewer: str) -> str:
        low, high = self.persona.answer_chars
        labels = ("面试官", "候选人") if self.language is Language.ZH else ("Interviewer", "Me")
        dialogue = "\n".join(
            f"{labels[0] if who == 'interviewer' else labels[1]}：{text}"
            for who, text in self.history[-HISTORY_TURNS:]
        )
        prompt = get_prompt_library().render(
            "eval_candidate",
            behavior=self.persona.behavior.strip(),
            min_chars=low,
            max_chars=high,
            language=self.language.value,
            resume=self.resume,
            dialogue=dialogue or "（面试刚开始）",
            interviewer=interviewer,
        )
        reply = await self.llm.complete(prompt.messages, max_tokens=high * 3, temperature=0.8)
        text = reply.text.strip()
        self.history.append(("interviewer", interviewer))
        self.history.append(("candidate", text))
        return text
