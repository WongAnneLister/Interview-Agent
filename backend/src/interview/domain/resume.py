"""简历原文校验、文字抽取和结构化解析。

只保留面试会用到的经历、项目、技能和教育。手机号、邮箱、地址等不进入结果
（模型没有这些字段；提示词也明确禁止提取）。
"""

from __future__ import annotations

import re
from io import BytesIO

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PasswordType, PdfReader

from interview.db.models import Resume, UsageRole
from interview.db.repository import UserScopedRepository
from interview.observability.tracing import span
from interview.prompts.loader import get_prompt_library
from interview.prompts.structured import StructuredResult, generate_structured
from interview.providers.base import LLMProvider

MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_RESUME_TEXT_CHARS = 50_000
# multipart 的边界和头部。原始请求体可以略大于文件，文件本身仍是 10 MB。
MAX_RESUME_UPLOAD_BODY = MAX_RESUME_BYTES + 64 * 1024
_PDF_MAGIC = b"%PDF-"
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9](?:[\s-]?\d){9}(?!\d)")


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


class ResumeTooLong(ResumeError):
    status_code = 422
    code = "resume_too_long"


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
    role: str = Field(default="", description="本人在项目中的角色，未注明时为空")
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


class ResumeReplacement(_Ignore):
    """整份替换解析结果。每个字段都要出现，缺字段不能靠默认值把已有内容清空。"""

    name: str
    work: list[WorkItem]
    projects: list[ProjectItem]
    skills: list[str]
    education: list[EducationItem]


class ResumeRepository(UserScopedRepository[Resume]):
    model = Resume


def _clean_text(text: str) -> str:
    return " ".join(_PHONE.sub("", _EMAIL.sub("", text)).split())


def _clean_list(items: list[str]) -> list[str]:
    return [item for item in (_clean_text(item) for item in items) if item]


def redact_contacts(parsed: ResumeParsed) -> ResumeParsed:
    """去掉自由文本里的邮箱和手机号。这些内容没有自己的字段，但可能被写进亮点或技能。"""
    work: list[WorkItem] = []
    for item in parsed.work:
        company, title = _clean_text(item.company), _clean_text(item.title)
        if not company or not title:
            continue
        work.append(
            item.model_copy(
                update={
                    "company": company,
                    "title": title,
                    "start": _clean_text(item.start),
                    "end": _clean_text(item.end),
                    "highlights": _clean_list(item.highlights),
                }
            )
        )
    projects: list[ProjectItem] = []
    for item in parsed.projects:
        name, role = _clean_text(item.name), _clean_text(item.role)
        if not name:
            continue
        projects.append(
            item.model_copy(
                update={
                    "name": name,
                    "role": role,
                    "tech_stack": _clean_list(item.tech_stack),
                    "highlights": _clean_list(item.highlights),
                }
            )
        )
    education: list[EducationItem] = []
    for item in parsed.education:
        school = _clean_text(item.school)
        if not school:
            continue
        education.append(
            item.model_copy(
                update={
                    "school": school,
                    "degree": _clean_text(item.degree),
                    "field": _clean_text(item.field),
                    "start": _clean_text(item.start),
                    "end": _clean_text(item.end),
                }
            )
        )
    return parsed.model_copy(
        update={
            "name": _clean_text(parsed.name),
            "work": work,
            "projects": projects,
            "skills": _clean_list(parsed.skills),
            "education": education,
        }
    )


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
        # 只设了所有者密码、用户密码为空的 PDF（常见于「限制编辑」）用空密码可以读
        if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise ResumeUnreadable("这份 PDF 已加密，无法读取文字")
        parts = [page.extract_text() or "" for page in reader.pages]
    except ResumeError:
        raise
    except Exception as exc:
        raise ResumeUnreadable("无法读取这份 PDF") from exc
    text = "\n".join(parts).strip()
    if not text:
        raise ResumeHasNoText("这份 PDF 没有可提取的文字，可能是扫描件。请上传可以选中文字的 PDF。")
    if len(text) > MAX_RESUME_TEXT_CHARS:
        raise ResumeTooLong("这份简历的文字太长，超过 5 万字，请删减后再上传。")
    return text


async def parse_resume(llm: LLMProvider, text: str) -> StructuredResult[ResumeParsed]:
    """用提示词 resume_parse v1 把简历正文解析成 ResumeParsed。"""
    prompt = get_prompt_library().render("resume_parse", resume_text=text)
    with span("resume.parse", role=UsageRole.RESUME_PARSE.value):
        result = await generate_structured(llm, prompt, ResumeParsed, max_tokens=4096)
    return StructuredResult(
        redact_contacts(result.value), result.prompt_id, result.prompt_version, result.retries
    )
