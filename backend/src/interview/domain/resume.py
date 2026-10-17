"""简历原文校验、文字抽取和结构化解析。

只保留面试会用到的经历、项目、技能和教育。手机号、邮箱、地址等不进入结果
（模型没有这些字段；提示词也明确禁止提取）。
"""

from __future__ import annotations

from io import BytesIO

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from interview.db.models import Resume, UsageRole
from interview.db.repository import UserScopedRepository
from interview.observability.tracing import span
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import StructuredResult, generate_structured
from interview.providers.base import LLMProvider

MAX_RESUME_BYTES = 10 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"


class ResumeError(Exception):
    """上传的简历无法接受。message 可以展示给用户。"""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResumeTooLarge(ResumeError):
    status_code = 413
    code = "file_too_large"


class ResumeNotPdf(ResumeError):
    status_code = 415
    code = "unsupported_media_type"


class ResumeHasNoText(ResumeError):
    status_code = 422
    code = "resume_no_text"


class ResumeUnreadable(ResumeError):
    status_code = 422
    code = "resume_unreadable"


class _Ignore(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WorkItem(_Ignore):
    company: str = Field(min_length=1, description="公司或组织名称")
    title: str = Field(min_length=1, description="职位")
    start: str = Field(default="", description="开始时间，保留简历原文")
    end: str = Field(default="", description="结束时间，保留简历原文；在职可写至今")
    highlights: list[str] = Field(default_factory=list[str], description="与工作相关的要点")


class ProjectItem(_Ignore):
    name: str = Field(min_length=1, description="项目名称")
    role: str = Field(min_length=1, description="本人在项目中的角色")
    tech_stack: list[str] = Field(default_factory=list[str], description="技术栈")
    highlights: list[str] = Field(default_factory=list[str], description="项目亮点")


class EducationItem(_Ignore):
    school: str = Field(min_length=1, description="学校")
    degree: str = Field(default="", description="学历")
    field: str = Field(default="", description="专业")
    start: str = Field(default="", description="开始时间，保留简历原文")
    end: str = Field(default="", description="结束时间，保留简历原文")


class ResumeParsed(_Ignore):
    """解析后的简历。没有手机号、邮箱、地址等字段。"""

    name: str = Field(default="", description="候选人姓名")
    work: list[WorkItem] = Field(default_factory=list[WorkItem], description="工作经历")
    projects: list[ProjectItem] = Field(default_factory=list[ProjectItem], description="项目经历")
    skills: list[str] = Field(default_factory=list[str], description="技能")
    education: list[EducationItem] = Field(
        default_factory=list[EducationItem], description="教育经历"
    )


class ResumeRepository(UserScopedRepository[Resume]):
    model = Resume


def check_resume_file(data: bytes) -> None:
    """按文件头判断是否为 PDF，并限制 10 MB。不看文件名和 Content-Type。"""
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeTooLarge("简历不能超过 10 MB")
    if not data.startswith(_PDF_MAGIC):
        raise ResumeNotPdf("只接受 PDF 文件")


def extract_text(data: bytes) -> str:
    """抽出 PDF 里的文字。扫描件没有文字层时返回明确错误。"""
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ResumeUnreadable("这份 PDF 已加密，无法读取文字")
        parts = [page.extract_text() or "" for page in reader.pages]
    except ResumeError:
        raise
    except Exception as exc:
        raise ResumeUnreadable("无法读取这份 PDF") from exc
    text = "\n".join(parts).strip()
    if not text:
        raise ResumeHasNoText("这份 PDF 没有可提取的文字，可能是扫描件。请上传可以选中文字的 PDF。")
    return text


async def parse_resume(llm: LLMProvider, text: str) -> StructuredResult[ResumeParsed]:
    """用提示词 resume_parse v1 把简历正文解析成 ResumeParsed。"""
    prompt = get_prompt_library().render("resume_parse", resume_text=text)
    with span("resume.parse", role=UsageRole.RESUME_PARSE.value):
        return await generate_structured(llm, prompt, ResumeParsed, max_tokens=4096)
