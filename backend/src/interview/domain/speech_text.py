"""把面试官要说的文字规范化成适合语音合成的形式；纯函数，规则表驱动。"""

from __future__ import annotations

import re

from interview.db.models import Language

_ZH_DIGITS = "零一二三四五六七八九"
_ZH_UNITS = {"ms": "毫秒", "min": "分钟", "s": "秒", "w": "万"}
_EN_UNITS = {"ms": "milliseconds"}
_EN_SECONDS = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(\s*)s(?![A-Za-z0-9])")
_DECADE = re.compile(r"\d{3}0")  # 英文 "1990s" "2010s" 是年代，不是秒

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*")
_LIST = re.compile(r"(?m)^[ \t]*(?:[-*•]|\d+[.)])[ \t]+")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_P_READING = re.compile(r"(?<![A-Za-z0-9])P(\d{2,3})(?![A-Za-z0-9])")
# 单位、缩写后面常紧跟汉字（“3ms左右”“P95延迟”），\b 在汉字前不成立，改为“后面不是 ASCII 字母数字”
_ASCII_END = r"(?![A-Za-z0-9])"
_ZH_LETTER_UNIT = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*([A-Za-z]+){_ASCII_END}")
_ZH_SLASH = re.compile(r"(?<=[\u4e00-\u9fff])\s*/\s*(?=[\u4e00-\u9fff])")
_TILDE = re.compile(r"(?<=\d)\s*~\s*(?=\d)")
_ARROW = re.compile(r"(?:->|=>)")


def _zh_integer(number: int) -> str:
    if number == 0:
        return _ZH_DIGITS[0]
    if number >= 10_000:
        return str(number)
    units = ("", "十", "百", "千")
    result = ""
    pending_zero = False
    for position in range(3, -1, -1):
        digit = number // 10**position % 10
        if digit == 0:
            pending_zero = bool(result)
            continue
        if pending_zero:
            result += _ZH_DIGITS[0]
            pending_zero = False
        result += _ZH_DIGITS[digit] + units[position]
    return result[1:] if result.startswith("一十") else result


def _zh_number(number: str) -> str:
    if "." not in number:
        return _zh_integer(int(number))
    whole, fraction = number.split(".", 1)
    digits = "".join(_ZH_DIGITS[int(digit)] for digit in fraction)
    return f"{_zh_integer(int(whole))}点{digits}"


def _markdown(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = _HEADING.sub("", text)
    return _LIST.sub("", text)


def _percent(text: str, language: Language) -> str:
    def replace(match: re.Match[str]) -> str:
        number = match.group(1)
        return f"百分之{_zh_number(number)}" if language == Language.ZH else f"{number} percent"

    return _PERCENT.sub(replace, text)


def _en_seconds(match: re.Match[str]) -> str:
    number, space = match.group(1), match.group(2)
    if not space and _DECADE.fullmatch(number):
        return match.group(0)
    return f"{number} seconds"


def _units(text: str, language: Language) -> str:
    units = _ZH_UNITS if language == Language.ZH else _EN_UNITS
    for suffix, replacement in units.items():
        pattern = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*{re.escape(suffix)}{_ASCII_END}")
        if language == Language.ZH:
            text = pattern.sub(lambda match, unit=replacement: f"{match.group(1)}{unit}", text)
        else:
            text = pattern.sub(lambda match, unit=replacement: f"{match.group(1)} {unit}", text)
    if language == Language.ZH:
        text = _ZH_LETTER_UNIT.sub(lambda match: f"{match.group(1)} {match.group(2)}", text)
    else:
        text = _EN_SECONDS.sub(_en_seconds, text)
    return text


def _abbreviations(text: str, language: Language) -> str:
    if language != Language.ZH:
        return text
    return _P_READING.sub(lambda match: f"P{_zh_number(match.group(1))}", text)


def _symbols(text: str, language: Language) -> str:
    if language == Language.ZH:
        text = text.replace("&", "和")
        text = _ZH_SLASH.sub("或", text)
        text = _TILDE.sub("到", text)
        return _ARROW.sub("到", text)
    text = text.replace("&", " and ")
    text = _TILDE.sub(" to ", text)
    return _ARROW.sub(" to ", text)


def normalize(text: str, language: Language) -> str:
    text = _markdown(text)
    text = _percent(text, language)
    text = _units(text, language)
    text = _abbreviations(text, language)
    text = _symbols(text, language)
    return re.sub(r"\s+", " ", text).strip()
