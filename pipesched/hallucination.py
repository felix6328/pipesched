"""Script-measurable hallucination flags for comment-blocker extraction.

Definition (applied to the RAW model JSON, before product guardrails):

1. ``citation_ungrounded`` — ``citation`` is non-empty but is not a contiguous
   substring of the source comment (after the same whitespace-flexible match
   used in :func:`pipesched.comments.grounded`).
2. ``blocker_without_grounded_citation`` — raw ``is_blocker`` is true, but the
   citation is empty or ungrounded. A blocker claim without a valid quote is
   treated as unsupported.
3. ``reason_invented_number`` — ``reason`` contains an Arabic numeral (or a
   decimal) that does not appear in the source comment. Invented quantities
   are unsupported claims.
4. ``reason_invented_quote`` — ``reason`` wraps a span of at least 4 characters
   in quotes (「」 "" ''), and that span is not a grounded substring of the
   source.

Paraphrases such as「技术实现存在明确障碍」that do not introduce numbers or
fabricated quotes are *not* flagged. That is conservative on purpose: a looser
lexical overlap rule would punish legitimate glosses.

The primary hallucination rate is the share of successfully parsed model
outputs that carry at least one of the flags above. Product output applies a
guardrail (ungrounded blockers are suppressed) so the *visible* rate can be
lower; both are reported.
"""

from __future__ import annotations

import re
from typing import Optional

from pipesched.comments import grounded


_QUOTE_RE = re.compile(r"[「「\"'\u201c\u2018](.{4,}?)[」\"'\u201d\u2019]")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def hallucination_flags(
    parsed: Optional[dict],
    source_text: str,
) -> list[str]:
    """Return a list of hallucination flag names for one raw model parse."""
    flags: list[str] = []
    if not parsed or not isinstance(parsed, dict):
        return flags
    source = source_text or ""
    citation = parsed.get("citation") or ""
    if not isinstance(citation, str):
        citation = str(citation) if citation is not None else ""
    reason = parsed.get("reason")
    if reason is None:
        reason = ""
    elif not isinstance(reason, str):
        reason = str(reason)
    is_blocker = bool(parsed.get("is_blocker"))
    cite_ok = bool(citation) and grounded(citation, source)

    if citation and not cite_ok:
        flags.append("citation_ungrounded")
    if is_blocker and not cite_ok:
        flags.append("blocker_without_grounded_citation")

    if reason:
        src_nums = set(_NUM_RE.findall(source))
        reason_nums = set(_NUM_RE.findall(reason))
        if reason_nums - src_nums:
            flags.append("reason_invented_number")
        for span in _QUOTE_RE.findall(reason):
            if span and not grounded(span, source):
                flags.append("reason_invented_quote")
                break
    return flags
