"""
Shared text-cleaning utility.

Claude's web_search tool sometimes has the model wrap sourced claims
in citation tags (e.g. <cite index="21-3">...</cite> or the
<cite ...> variant) even when the system prompt asks for plain
JSON only. Since these agents parse the response as strict JSON and
store the text fields directly (reasoning, headlines, etc.), any
leftover tags would end up stored and later shown as-is on the
dashboard. This strips the tags while keeping the enclosed text —
we want the cited claim, not the citation markup.
"""

import re

_CITE_TAG_RE = re.compile(r"</?(?:antml:)?cite(?:\s+[^>]*)?>", re.IGNORECASE)


def strip_citation_tags(text: str) -> str:
    if not text:
        return text
    return _CITE_TAG_RE.sub("", text)


def clean_opinion_text_fields(parsed: dict) -> dict:
    """Recursively strip citation tags from every string value in a
    parsed agent opinion dict (reasoning, key_data entries, etc.)."""

    def _clean(value):
        if isinstance(value, str):
            return strip_citation_tags(value)
        if isinstance(value, list):
            return [_clean(v) for v in value]
        if isinstance(value, dict):
            return {k: _clean(v) for k, v in value.items()}
        return value

    return _clean(parsed)
