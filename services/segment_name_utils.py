"""Display helpers for the segments_quarterly.segment_name column.

Indian XBRL filings often dump full disclosure sentences into
DescriptionOfSingleSegment ("The Company operates only in one segment i.e.
manufacture, sale and service of X") which then land verbatim in the
segment_name column. Those sentences are unusable as UI labels.

`clean_segment_name` collapses any such boilerplate to a tidy "Operations"
label. Short legitimate names ("Banking", "Steel") pass through untouched.

This is a DISPLAY-LAYER helper — classification/keyword-matching code
that needs the raw text should continue to query the column directly.
"""
from __future__ import annotations

import re


_SINGLE_SEGMENT_PATTERNS = (
    re.compile(r"^\s*(?:the\s+)?company\b", re.IGNORECASE),
    re.compile(
        r"\b(?:one|single|sole)\s+"
        r"(?:operating\s+|business\s+|reportable\s+|primary\s+)?segment\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bonly\s+(?:one|single)\s+segment\b", re.IGNORECASE),
)


def clean_segment_name(name):
    if not name or not isinstance(name, str):
        return name
    trimmed = name.strip()
    if not trimmed:
        return trimmed
    for pat in _SINGLE_SEGMENT_PATTERNS:
        if pat.search(trimmed):
            return "Operations"
    return trimmed
