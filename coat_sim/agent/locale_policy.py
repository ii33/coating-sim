"""Language policy for visible assistant text, independent of Qt and providers.

The desktop passes ``QLocale.system().bcp47Name()`` for each request. Headless
entry points use the process locale without changing its global settings. This
module never translates provider output or creates missing reasoning text.
"""

from __future__ import annotations

import locale
import os
import re
from collections.abc import Mapping
from typing import Any


_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_LANGUAGES = {
    "ar": "العربية (Arabic)",
    "cs": "čeština (Czech)",
    "da": "dansk (Danish)",
    "de": "Deutsch (German)",
    "el": "Ελληνικά (Greek)",
    "en": "English",
    "es": "español (Spanish)",
    "fi": "suomi (Finnish)",
    "fr": "français (French)",
    "he": "עברית (Hebrew)",
    "hi": "हिन्दी (Hindi)",
    "hu": "magyar (Hungarian)",
    "id": "Bahasa Indonesia (Indonesian)",
    "it": "italiano (Italian)",
    "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)",
    "nl": "Nederlands (Dutch)",
    "no": "norsk (Norwegian)",
    "pl": "polski (Polish)",
    "pt": "português (Portuguese)",
    "ro": "română (Romanian)",
    "ru": "русский (Russian)",
    "sv": "svenska (Swedish)",
    "th": "ไทย (Thai)",
    "tr": "Türkçe (Turkish)",
    "uk": "українська (Ukrainian)",
    "vi": "Tiếng Việt (Vietnamese)",
}


def _language_tag(value: Any) -> str:
    """Accept bounded locale identifiers, never arbitrary prompt text."""
    if not isinstance(value, str) or len(value) > 64:
        return ""
    tag = value.strip().split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    if tag in ("C", "POSIX"):
        return "en"
    if not _TAG.fullmatch(tag):
        return ""
    parts = tag.split("-")
    parts[0] = parts[0].lower()
    for index, part in enumerate(parts[1:], start=1):
        if len(part) == 4 and part.isalpha():
            parts[index] = part.title()
        elif len(part) == 2 and part.isalpha():
            parts[index] = part.upper()
        else:
            parts[index] = part.lower()
    return "-".join(parts)


def system_language(config: Mapping[str, Any] | None = None) -> str:
    """Return the request's system language, with a portable headless fallback.

    The request value is transient, so changing OS language does not require a
    settings-file migration. ``display_locale`` accepts an older caller name.
    Language environments apply only when the UI has not supplied its locale.
    """
    if config is not None:
        for key in ("assistant_system_language", "display_locale"):
            if tag := _language_tag(config.get(key)):
                return tag
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if tag := _language_tag(os.environ.get(key)):
            return tag
    categories = [locale.LC_CTYPE]
    if hasattr(locale, "LC_MESSAGES"):
        categories.insert(0, locale.LC_MESSAGES)
    for category in categories:
        try:
            name = locale.getlocale(category)[0]
        except (ValueError, TypeError, locale.Error):
            continue
        if tag := _language_tag(name):
            return tag
    return "en"


def language_instructions(config: Mapping[str, Any] | None = None) -> str:
    """Request one language consistently across main and delegated agents.

    This is a prompt preference, not a guarantee that every provider exposes or
    obeys a language setting for reasoning. It adds no unsupported API options.
    File paths, schema keys, code and factual references remain exact.
    """
    tag = system_language(config)
    language = tag.split("-", 1)[0]
    if language == "zh":
        traditional = "Hant" in tag.split("-") or bool(
            {"TW", "HK", "MO"}.intersection(tag.split("-")))
        target = "繁體中文" if traditional else "简体中文"
        return (
            f"\n本轮系统默认语言：{target}（{tag}）。主助手、执行子代理、分析助手和上下文整理"
            f"的可见回复、简短过程说明，以及服务公开返回的可显示思考文字，默认都使用{target}。"
            "用户明确要求某种语言时遵循该要求；翻译任务保留目标语言；代码、路径、参数键、"
            "专有名称与引用原文保持准确，不为语言统一修改工具参数。此运行时语言设置优先于"
            "人设文本里固定中文或其他语言的历史表达偏好，但不改变软件能力、权限与用户约束。"
            "语言约束只适用于服务可公开返回的文字，不要求披露隐藏推理，也不得编造未返回的"
            "思考或将内部隐含推理当作公开过程。服务未返回可显示思考时直接继续任务。\n"
        )
    target = _LANGUAGES.get(language, f"the language identified by BCP 47 locale {tag}")
    return (
        f"\nThe default system language for this run is {target} ({tag}). Use this language "
        "consistently for visible responses and brief progress explanations from the main assistant, "
        "execution delegates, analysts and context summaries, and for any displayable thinking text "
        "that the provider publicly returns. Follow an explicit user language request; preserve the "
        "target language in translation tasks. Keep code, file paths, schema keys, proper names and "
        "quoted source text exact; never change tool arguments just to localize them. This runtime "
        "language preference overrides historical fixed-language phrasing in the persona, without "
        "changing capabilities, permissions or user constraints. It governs public text only, does "
        "not request disclosure of private reasoning, and must not invent missing thinking text. "
        "If the provider supplies no displayable thinking, continue the task normally.\n"
    )


output_language_instruction = language_instructions
