"""
Verdict extractor — pulls a structured JSON summary out of the model's
8-section markdown report.

We require the model to emit a fenced JSON block at the END of its output
(see prompts.py — the closing instruction). This module finds and parses
that block. If the model forgot or emitted malformed JSON we fall back to
regex heuristics over the markdown so we always have *something* structured
to filter / sort / aggregate on.

Why structured verdicts matter
------------------------------
Without this, every report is a markdown blob — you can't filter "show me
all CATCH retrospectives where score_delta > 3" or "all forward BUYs with
A-tier conviction this month". With this, Movers Analysis (Feature A) can
roll up across 50 stocks in a batch.

Returned shape:
{
    "stance": "catch" | "late" | "miss" | "false_positive"           (retrospective)
              | "buy" | "watch" | "avoid",                          (forward)
    "conviction": "A" | "B" | "C" | None,
    "headline": "<= 200 char one-liner",
    "alpha_pp": 23.4,
    "pe_rerating_pct": 31.0,
    "score_setup": 5.4,
    "score_end": 8.1,
    "score_delta": 2.7,
    "top_risk": "<= 200 char",
    "data_gaps": int,
    "extracted_via": "json_block" | "regex_fallback" | "mixed",
}
"""
from __future__ import annotations

import json
import re
from typing import Any


_VALID_RETRO_STANCES = {"catch", "late", "miss", "false_positive"}
_VALID_FORWARD_STANCES = {"buy", "watch", "avoid"}
_VALID_CONVICTIONS = {"A", "B", "C"}


def extract_verdict(
    *, markdown: str, mode: str, dossier: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a structured verdict dict; never raises."""
    out: dict[str, Any] = {
        "stance": None,
        "conviction": None,
        "headline": None,
        "alpha_pp": None,
        "pe_rerating_pct": None,
        "score_setup": None,
        "score_end": None,
        "score_delta": None,
        "top_risk": None,
        "data_gaps": 0,
        "extracted_via": "regex_fallback",
    }

    # Hydrate numeric fields from the dossier (these are deterministic — the
    # model doesn't need to compute them, we just want them on the verdict
    # row for filtering).
    if dossier:
        bm = (dossier.get("benchmark") or {})
        if bm.get("alpha_pp") is not None:
            out["alpha_pp"] = float(bm["alpha_pp"])
        val = (dossier.get("valuation") or {})
        if val.get("pe_rerating_pct") is not None:
            out["pe_rerating_pct"] = float(val["pe_rerating_pct"])
        score = dossier.get("valvo_score") or {}
        if score.get("setup") and score["setup"].get("final_score") is not None:
            out["score_setup"] = float(score["setup"]["final_score"])
        if score.get("end") and score["end"].get("final_score") is not None:
            out["score_end"] = float(score["end"]["final_score"])
        if (score.get("delta") or {}).get("final_score") is not None:
            out["score_delta"] = float(score["delta"]["final_score"])

    # 1) Try fenced JSON block tagged ```verdict
    json_block = _find_verdict_json_block(markdown)
    if json_block:
        try:
            parsed = json.loads(json_block)
            if isinstance(parsed, dict):
                if parsed.get("stance"):
                    out["stance"] = str(parsed["stance"]).strip().lower()
                if parsed.get("conviction"):
                    out["conviction"] = str(parsed["conviction"]).strip().upper()
                if parsed.get("headline"):
                    out["headline"] = str(parsed["headline"])[:240]
                if parsed.get("top_risk"):
                    out["top_risk"] = str(parsed["top_risk"])[:240]
                out["extracted_via"] = "json_block"
        except json.JSONDecodeError:
            pass

    # 2) Regex fallbacks for fields the model didn't emit
    if not out["stance"]:
        out["stance"] = _guess_stance(markdown, mode)
        if out["stance"] and out["extracted_via"] == "json_block":
            out["extracted_via"] = "mixed"

    if not out["headline"]:
        out["headline"] = _guess_headline(markdown)
        if out["headline"] and out["extracted_via"] == "json_block":
            out["extracted_via"] = "mixed"

    if not out["top_risk"]:
        out["top_risk"] = _guess_top_risk(markdown)

    out["data_gaps"] = _count_data_gaps(markdown)

    # Validate stance against mode
    if out["stance"]:
        valid = _VALID_RETRO_STANCES if mode == "retrospective" else _VALID_FORWARD_STANCES
        if out["stance"] not in valid:
            out["stance"] = None
    if out["conviction"] not in _VALID_CONVICTIONS:
        out["conviction"] = None

    return out


# ═══════════════════════════════════════════════════════════════════════════
#  JSON block parser
# ═══════════════════════════════════════════════════════════════════════════

# Match ```verdict ... ``` or ```json (when the report has a single JSON block at the end)
_VERDICT_FENCE_RE = re.compile(
    r"```\s*(?:verdict|json)\s*\n(?P<body>.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def _find_verdict_json_block(md: str) -> str | None:
    if not md:
        return None
    matches = list(_VERDICT_FENCE_RE.finditer(md))
    if not matches:
        return None
    # Prefer the LAST fenced block (the model is told to put it at the end)
    return matches[-1].group("body").strip()


# ═══════════════════════════════════════════════════════════════════════════
#  Heuristic fallbacks
# ═══════════════════════════════════════════════════════════════════════════

_RETRO_STANCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("catch",          re.compile(r"\b(catch|caught|would have caught)\b", re.I)),
    ("late",           re.compile(r"\b(late|missed (?:the )?early|chased)\b", re.I)),
    ("miss",           re.compile(r"\b(miss(?:ed)?|did not (?:catch|see))\b", re.I)),
    ("false_positive", re.compile(r"\b(false positive|whipsaw|head[- ]fake)\b", re.I)),
]

_FORWARD_STANCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("buy",   re.compile(r"\b(buy|accumulate|re-rating candidate)\b", re.I)),
    ("watch", re.compile(r"\b(watch|wait for trigger|on the bench)\b", re.I)),
    ("avoid", re.compile(r"\b(avoid|sell|trim|pass)\b", re.I)),
]


def _guess_stance(md: str, mode: str) -> str | None:
    if not md:
        return None
    bottom_line = _extract_section(md, "1. Bottom Line", max_chars=1200)
    if not bottom_line:
        bottom_line = md[:2000]
    patterns = _RETRO_STANCE_PATTERNS if mode == "retrospective" else _FORWARD_STANCE_PATTERNS
    for label, pat in patterns:
        if pat.search(bottom_line):
            return label
    return None


def _guess_headline(md: str) -> str | None:
    bl = _extract_section(md, "1. Bottom Line", max_chars=600)
    if not bl:
        return None
    # First sentence of bottom line, up to ~200 chars.
    bl = re.sub(r"\s+", " ", bl).strip()
    sentence = re.split(r"(?<=[.!?])\s", bl, maxsplit=1)[0]
    return sentence[:240] if sentence else None


def _guess_top_risk(md: str) -> str | None:
    risks = _extract_section(md, "8.", max_chars=2400) or _extract_section(md, "Risks", max_chars=2400)
    if not risks:
        return None
    m = re.search(r"\[HIGH\][^\n]{5,300}", risks)
    if m:
        return m.group(0).strip()[:240]
    # Fall back to first bulleted risk line
    m = re.search(r"^[-*]\s+([^\n]{20,260})", risks, re.MULTILINE)
    if m:
        return m.group(1).strip()[:240]
    return None


_DATA_GAP_RE = re.compile(r"\bdata gap\b", re.I)


def _count_data_gaps(md: str) -> int:
    if not md:
        return 0
    return len(_DATA_GAP_RE.findall(md))


def _extract_section(md: str, header_token: str, max_chars: int = 2000) -> str | None:
    """Find a section starting with `## <header_token>` (case-insensitive) and
    return its body up to the next H2."""
    if not md:
        return None
    pattern = re.compile(
        r"^##\s+" + re.escape(header_token) + r".*?(?=\n##\s+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(md)
    if not m:
        return None
    body = m.group(0)
    return body[:max_chars]
