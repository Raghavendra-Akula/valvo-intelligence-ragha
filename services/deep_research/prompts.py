"""
Prompt templates for Deep Research.

The model receives:
  • a SYSTEM prompt setting the institutional-equity-research persona,
    structural rules, and forbidden behaviors (no fabrication, no
    consensus mining beyond the dossier).
  • a USER prompt containing the JSON dossier + the 8-section template
    for the requested mode (retrospective vs forward).

Both modes share the same skeleton so the front-end renderer can
treat them identically; they differ only in the verb tense and the
"verdict" framing.
"""
from __future__ import annotations

import json
from typing import Any


SYSTEM_PROMPT = """You are a senior equity research analyst writing an institutional-grade single-stock report for a sophisticated buy-side audience. You have deep familiarity with Indian listed equities, sector dynamics, and quarterly fundamentals.

You have two evidence sources:
  • the **dossier** (JSON block in the user prompt) — first-party facts pulled from our database (quarterly P&L, balance sheet, cash flow, shareholding history, peers, filings, corporate actions, **VALVO 12-parameter scoring trace**, **three cohort comparisons**, **as-of-date business snapshot**). Treat these as ground truth.
  • **Google Search** (web grounding) — actively use this to enrich the dossier with: management commentary from recent earnings calls, brokerage opinions, news on order wins / capex / regulatory changes, sector trends, and any material event the dossier doesn't capture.

Rules of evidence (non-negotiable):
1. **Use both sources.** A great report fuses internal numbers with external context. Don't just summarise the dossier — research the company.
2. **Cite the web.** Every web-derived fact (management quote, brokerage note, news event, sector datum) must be attributable to a search result. The grounding system records URLs automatically — write so a reader can tell which claims came from web vs dossier.
3. **Numbers come from the dossier first.** If the dossier has a number, use it. Only fall back to the web for numbers when the dossier is silent — and then say so.
4. **No fabrication.** If neither source supports a claim, write "Data gap:" and name what would be needed. Never invent customer names, order values, dates, or quotes.
5. **Lead with the bottom line.** The first paragraph must answer "what happened / what could happen and why" in 4-6 sentences.
6. **Be specific.** "Revenue +27% YoY to ₹412 Cr" beats "strong growth". Name filings ("Q3FY25 result filed 2025-01-28"), brokerages, executives.
7. **Tag every risk as HIGH / MEDIUM / LOW.** Risks must be specific to this company's actual exposure, not generic market risks.
8. **Tone: precise, calm, analytical.** No marketing language. No hedging clichés. No emoji except the 🟢 / 🟡 / 🔴 markers explicitly required in section 4.
9. **Format: GitHub-flavoured Markdown.** Every one of the 8 section headers MUST start on its own line with `## ` (H2). Sub-sections use `### ` (H3). Use tables for quarterly trajectories and peer comparisons. No HTML.

   Correct:
   ```
   ## 1. Bottom Line
   Re-rating candidate. The stock has...

   ## 2. Where We Are Now
   - Last close: ...
   ```

   Wrong (will render as one paragraph and fail review):
   ```
   1. Bottom Line Re-rating candidate. The stock has...
   2. Where We Are Now - Last close: ...
   ```

10. **Use the VALVO scoring trace.** The dossier carries a `valvo_score` block with the system's own 12-parameter score at SETUP (day before window) and END (last day of window). Reference it explicitly — name the setup score, the end score, the delta, and call out the 2-3 individual parameters whose ratings changed most. This is what tells the reader whether the system would have caught the move in advance.
11. **Use the three cohort comparisons.** The dossier carries `cohorts.sub_sector`, `cohorts.primary_theme`, `cohorts.primary_wave` — each with avg/median/top/bottom ROC, the stock's percentile inside the cohort, and the stock's alpha vs the cohort average. Use these to attribute the move correctly: was the stock leading its sub-sector (idiosyncratic), riding a theme (e.g. Atmanirbhar Bharat), or just a bystander on a wave rotation? **Specify the level**, don't just say "outperformed peers".
12. **For retrospective reports, use the AS-OF block.** `business_as_of.latest_annual` and `business_as_of.shareholding` reflect what the business and ownership looked like AT the end of the window — not today. When the move was 12+ months in the past, anchor your fundamental commentary to those numbers, not to the current `business_profile` snapshot. (For forward mode, `business_profile` is the right snapshot.)
13. **End with a structured VERDICT block.** After section 8, emit a final fenced JSON code block tagged ` ```verdict ` containing exactly these fields (see template). This drives Movers Analysis aggregation — without it your report cannot be filtered or compared with others.
14. **After the verdict block, also emit a PDF JSON block.** Schema and rules are spelled out at the bottom of the user prompt under "PDF JSON BLOCK". Fenced exactly as ` ```json:report-data ... ``` ` and placed as the very last thing in your response, immediately after the verdict block. This drives the polished downloadable PDF artifact — without it the user has to fall back on the markdown view alone.

You must produce all 8 sections in order, then the verdict block, then the PDF JSON block. Do not skip sections — if a section has no supporting data even after web search, fill it with a "Data gap:" note explaining what was unavailable."""


# ─────────────────────────────────────────────────────────────────────────────
# PDF JSON schema (also rendered into the user prompt so the model has it
# in-context). Keys with ?? are optional; everything else is required when the
# model has any signal at all. If a section is genuinely unsupported by the
# data, the model should still emit the key with a "Data gap" placeholder so
# the renderer doesn't fall back to skeleton output.
# ─────────────────────────────────────────────────────────────────────────────
PDF_JSON_SCHEMA = """### PDF JSON BLOCK — required at end of response

After section 8, output exactly one fenced JSON block, fenced as:

```json:report-data
{ ... }
```

Allowed colours (use these exact strings) for any "color" field:
  "navy"  "info"  "pos"  "neg"  "accent"  "accent_dk"  "mute"

Schema (all keys required unless marked optional):

{
  "header": {
    "company_name": "ALL CAPS company name (max 40 chars)",
    "ticker": "primary ticker, e.g. MTARTECH",
    "isin": "ISIN code if known, else ''",
    "exchange_codes": "e.g. 'NSE / BSE 543270' (omit if unknown)",
    "sector": "short sector label",
    "tags": ["2-4 short ALL-CAPS theme/sub-sector tags, max 22 chars each"],
    "rank_label": "small label that appears at top of cover, e.g. 'EQUITY RESEARCH · DEEP RESEARCH'"
  },
  "window": "human-readable window string, e.g. '02-Feb-2026 → 30-Apr-2026'",
  "hero": {
    "value": "the single biggest number on the cover, e.g. '+112.9%'",
    "caption": "one-line context, e.g. '3-month price return · 02-Feb-26 → 30-Apr-26'"
  },
  "cover_kpis": [
    /* exactly 4 tiles */
    {"value": "₹3,033", "label": "Start", "sub": "02-Feb-26", "color": "navy"},
    {"value": "₹6,457", "label": "End",   "sub": "30-Apr-26", "color": "navy"},
    {"value": "+104pp", "label": "Alpha vs Smallcap 100",      "color": "pos"},
    {"value": "₹9,330 Cr", "label": "Initial Market Cap",      "color": "accent"}
  ],
  "tldr_html": "1 paragraph (4-6 sentences) summarising the report. Inline <b>bold</b> for key numbers/themes is allowed. No other HTML.",
  "dashboard": {
    "headline": "short title, e.g. 'Move Decomposition'",
    "lead": "1 short paragraph contextualising the at-a-glance view",
    "kpi_rows": [
      /* up to 2 rows of up to 4 tiles each. Same shape as cover_kpis. */
      [ {"value":"+112.9%","label":"Stock ROC","color":"pos"}, ... ]
    ],
    "snapshot_rows": [
      /* up to 8 rows of [metric, value]. Plain text, no HTML. */
      ["Initial market cap", "₹9,330 Cr"],
      ["Sub-sector / Wave", "Precision Engineering / Energy Transition"]
    ],
    "snapshot_footer_html": "1 short closing sentence (HTML <b> allowed)"
  },
  "pillars_lead": "optional; one short sentence introducing the trio",
  "pillars": [
    /* exactly 3 pillars */
    {
      "num": 1,
      "kicker": "ALL-CAPS sub-label, e.g. 'STRUCTURAL THESIS · LARGEST DRIVER'",
      "title": "the pillar title (no all-caps)",
      "color": "info",  /* navy/info/pos/neg/accent/accent_dk */
      "lead": "1-2 sentences framing the pillar",
      "bullets": [
        "3-5 bullets. <b>Bold</b> the key fact in each bullet. No other HTML.",
        "..."
      ]
    }
  ],
  "timeline_headline": "optional; defaults to 'In-Window Catalyst Timeline'",
  "timeline_lead": "optional; one short sentence",
  "timeline_legend": [
    /* 2-4 legend chips */
    {"label": "AI / Bloom",     "color": "info"},
    {"label": "Nuclear / SMR",  "color": "pos"},
    {"label": "Earnings",       "color": "accent_dk"},
    {"label": "Filings",        "color": "navy"}
  ],
  "timeline": [
    /* 6-12 events in chronological order */
    {
      "date_top": "Late Jan",     /* short date label, line 1 */
      "date_bot": "2026",         /* line 2, e.g. year */
      "event": "1-line event headline (no HTML)",
      "desc":  "1-line interpretation (no HTML)",
      "color": "accent_dk"
    }
  ],
  "quarterly": {
    "headline": "e.g. 'Quarterly Trajectory'",
    "lead": "1-2 sentence narrative",
    "chart": {
      /* the headline metric (usually revenue ₹ Cr). 3-6 quarters. */
      "labels": ["Q1FY26", "Q2FY26", "Q3FY26"],
      "values": [157, 136, 278],
      "title": "Quarterly revenue (₹ Cr)"
    },
    "table_headers": ["Metric", "Q1FY26", "Q2FY26", "Q3FY26", "YoY Q3"],
    "table_rows": [
      ["Revenue (₹ Cr)", "157", "136", "278", "+59%"],
      ["OPM %",          "18.1%", "12.5%", "23.0%", "+500 bps"]
    ],
    "footer_html": "1 short paragraph; <b>bold</b> allowed"
  },
  "rerating": {
    "headline": "e.g. 'The Re-rating Math'",
    "lead": "1-2 sentence narrative",
    "left_label":  "e.g. 'Start · PE (TTM)'",
    "left_value":  "e.g. '147x'",
    "right_label": "e.g. 'End · PE (TTM)'",
    "right_value": "e.g. '313x'",
    "change_label": "e.g. 'Re-rating'",
    "change_value": "e.g. '+113%'",
    "change_color": "pos",  /* pos / neg / accent */
    "footer_html": "1 short paragraph"
  },
  "risks_headline": "optional; defaults to 'What is Priced In'",
  "risks": [
    /* 3-5 risks, each with severity exactly 'HIGH', 'MEDIUM' or 'LOW' */
    {"label": "Multiple compression", "severity": "HIGH", "detail": "..."}
  ],
  "data_gaps": [
    "1-line bullets describing missing data the analyst flagged"
  ],
  "sources": [
    /* 6-20 sources. Use only URLs your web search actually returned. */
    {"title": "MTAR Q3 FY26 — Multibagg", "url": "https://..."}
  ]
}

CRITICAL:
  • The JSON must be valid (parseable). No trailing commas, no unquoted keys.
  • Tile and pillar text inside HTML fields may use <b>, <i> only. No other tags.
  • Use ₹ for INR amounts. Numbers in tiles should already include unit (Cr, %, x, pp).
  • If a section is unsupported by the data, still emit the key with a placeholder (e.g. risk severity 'LOW' + label 'Data gap', sources [], etc.) — never omit the key.
"""


_RETROSPECTIVE_TEMPLATE = """## 1. Bottom Line
4-6 sentences. What was the move (% and ₹), over what window, and the single sharpest reason it happened. Lead with the result, not the setup. End with one sentence stating whether you would classify this as **CATCH** (system + a competent operator would have caught the setup), **LATE** (signal showed up but only after a chunk of the move was gone), **MISS** (no system signal at all — moved on something the framework can't see), or **FALSE_POSITIVE** (system would have signalled but the move turned out hollow). State whether the move is earnings-driven, multiple-driven, or narrative-driven.

## 2. Move Decomposition + Cohort Attribution
- Window, start/end close, ROC %, max drawup %.
- Trading days, average daily turnover.
- Smallcap-100 ROC and the resulting alpha (pp).
- **Three-cohort attribution table.** Use `cohorts.sub_sector`, `cohorts.primary_theme`, `cohorts.primary_wave` from the dossier — render as a markdown table with columns: Cohort level | Cohort name | Cohort avg ROC % | Stock alpha vs cohort (pp) | Stock percentile in cohort | n. Then one sentence: at which level (sub-sector / theme / wave) did this stock most outperform — i.e. was the move idiosyncratic, theme-driven, or just a wave rotation?
- A compact "snapshot" table: Sector, Sub-sector, Theme, Wave, ISIN, NSE/BSE codes, market cap (search if not in dossier).

## 3. VALVO Scoring Trace (would the system have caught it?)
This is the most important diagnostic section. The dossier's `valvo_score` block carries the system's 12-parameter score at SETUP (day -1 of the window) and at END (last day of window).

- One sentence stating: setup final score X.X (rating R), end final score Y.Y (rating R), delta +/-Z.Z.
- A markdown table with columns: Parameter | Setup score | End score | Delta | Reasoning. Pull every parameter from `valvo_score.setup.scores` and `valvo_score.end.scores` and the matching `valvo_score.*.reasoning` lines. Order by absolute delta (largest moves first). Include all 12 parameters even if delta is 0.
- A second table for gatekeepers: Liquidity gate, Market-cap gate, Linearity gate — show pass/fail at setup vs end and the combined gatekeeper multiplier.
- 2-3 sentences interpreting what the trace tells us:
  - Was the setup score already high enough to act on at day -1? If yes, this is a CATCH-able setup.
  - Which parameters did the heaviest lifting — RS, sector_strength, fundamentals, IP, linearity?
  - If the setup score was low and the end score is high, name the parameters that flipped — that's the system's blind spot for this kind of move.

## 4. Catalyst Trio (the three pillars)
Identify the 3 dominant drivers of the move and write them as numbered pillars (### Pillar 1, ### Pillar 2, ### Pillar 3). For each pillar:
- A 1-line headline.
- 3-5 bullets of supporting evidence — fuse dossier facts (filings, quarterly trajectory, segment trends, balance sheet movement, shareholding change) with web research (management commentary from concall transcripts, brokerage notes, press coverage of order wins or capex announcements).
- One sentence on persistence (transient kicker vs durable re-rating fuel).

## 5. In-Window Timeline
Chronological list of every material event inside the window: quarterly results filed, large filings, corporate actions, management interviews, brokerage upgrades/downgrades, sector news that moved the stock. Each row: date | event type | one-line interpretation. Color-code interpretations: 🟢 confirming, 🟡 mixed, 🔴 disconfirming. (These are the only emojis allowed.)

## 6. Quarterly Trajectory, As-Of Fundamentals & Working-Capital Health
- Markdown table of the last 8 quarters: Period, Revenue (₹ Cr), Revenue YoY %, OPM %, Net Profit (₹ Cr), EPS.
- 2-3 sentences interpreting the trend.
- **As-of-window fundamentals:** Use the dossier's `business_as_of.latest_annual` block — these are the FY-level numbers as the market saw them at the end of the window (NOT today's snapshot). Report ROE, ROCE, debt/equity, FCF, capex run-rate from this block. Comment on how that fundamental shape supported (or didn't) the price move.
- A short table or bullets on balance-sheet movement during the window: change in debt, change in cash, working-capital cycle (receivables / inventory days), capex run-rate from the dossier's `annual` block.

## 7. Ownership Pulse
- **As-of-window shareholding** from `business_as_of.shareholding`: promoter %, FII %, DII %, public %, promoter pledge % at the end of the window.
- Direction of change over the dossier's `shareholding` history (did FIIs/DIIs add or trim during the move? did pledge change?).
- One sentence on what the ownership pattern suggests (accumulation, distribution, neutral) — and whether smart money was already positioned at the start of the window.

## 8. Peer Comparison, Valuation & Risks
- Top 3 peers from the dossier and their window ROC. Was this idiosyncratic or part of a peer pack? Cross-reference your finding here with the cohort attribution from section 2 — do they tell the same story?
- Valuation: TTM EPS at start vs end (`valuation.ttm_eps_start/end`), P/E at start vs end (`valuation.pe_start/end`), P/E re-rating % (`valuation.pe_rerating_pct`).
- Decompose the move into: earnings growth %, multiple expansion %, residual.
- Use web search to add: where current P/E sits vs the stock's own 5-year median (if you can find it), and how the multiple compares to the named peers today.
- 3-5 risks ranked [HIGH] / [MEDIUM] / [LOW]. Each: "[HIGH] Customer concentration — top 1 segment is X%, contract renewal due in Y." Specific to this stock.
- Bulleted list of every "Data gap:" you flagged anywhere in the report.
- Source URLs: dossier sources (BSE / Screener / company site) plus the web pages your grounded claims came from.

```verdict
{
  "stance": "catch | late | miss | false_positive",
  "conviction": "A | B | C",
  "headline": "<= 200 char one-liner capturing the verdict",
  "top_risk": "<= 200 char one-liner naming the single biggest risk to this stance"
}
```
"""


_FORWARD_TEMPLATE = """## 1. Bottom Line
4-6 sentences. What's the forward thesis (or anti-thesis) on this stock over the next 4-8 quarters? Lead with the verdict (BUY: re-rating candidate / WATCH: wait for trigger / AVOID: pass), then the 1-2 sharpest reasons. End with the one signal you'd watch most carefully to confirm or kill the thesis.

## 2. Where We Are Now + Cohort Setup
- Last close, distance from 52w high, recent 6-month ROC vs Smallcap-100.
- Current trading volume / liquidity, market cap (search if not in dossier).
- Sector, sub-sector, theme, wave membership.
- **Three-cohort setup table.** Use `cohorts.sub_sector`, `cohorts.primary_theme`, `cohorts.primary_wave` — render as a markdown table with columns: Cohort level | Cohort name | Cohort avg ROC % | Stock alpha vs cohort (pp) | Stock percentile | n. Then one sentence: is the stock leading, lagging, or in line with each level — i.e. is there room left in the move, or has the cohort already run?
- Latest brokerage stance / target price if reported in the past 90 days (web search).
- ISIN, NSE/BSE codes for reference.

## 3. VALVO Score: Where the System Stands Today
The dossier's `valvo_score.end` carries the live 12-parameter score (the `setup` slot here represents 6 months ago — so the delta tells you whether the system has been getting more or less convicted).

- One sentence stating: today's final score X.X (rating R), 6 months ago Y.Y (rating R), delta +/-Z.Z.
- A markdown table with columns: Parameter | Score now | Score 6m ago | Delta | Reasoning. Pull from `valvo_score.end.scores` (now) and `valvo_score.setup.scores` (6m ago) plus the matching `reasoning` lines. Order by absolute delta first.
- Gatekeepers table: Liquidity / Market-cap / Linearity — pass/fail today and combined multiplier.
- 2-3 sentences interpreting:
  - At today's score and rating, is this an actionable setup or a "watch"?
  - If improving (delta > 0), which parameters are the engine — is the improvement durable (fundamentals/RS) or fragile (price-only)?
  - If deteriorating, which parameters flipped negative and what would need to mean-revert for the score to climb back?

## 4. Forward Catalyst Trio
Three pillars (### Pillar 1, ### Pillar 2, ### Pillar 3) that could drive the next move. For each pillar:
- A 1-line headline.
- 3-5 bullets of evidence — fuse dossier (recent quarterly trajectory, segment mix shift, recent filings hinting at order wins / capex / new business, balance-sheet movement) with web research (management guidance from the most recent concall, sector tailwinds, competitor commentary, regulatory tailwinds/headwinds).
- One sentence on observability — what specific data point in the next 1-2 prints would confirm or invalidate this pillar.

## 5. Recent Timeline (last 6 months)
Chronological events from the dossier and web: results, filings, corporate actions, management interviews, brokerage actions, sector developments. Frame each event as "what this suggests about the next print". Use 🟢 / 🟡 / 🔴 for forward implication.

## 6. Quarterly Trajectory & Balance Sheet Trajectory
- Markdown table of last 8 quarters: Period, Revenue (₹ Cr), Revenue YoY %, OPM %, Net Profit (₹ Cr), EPS.
- 2-3 sentences on whether trends are accelerating, decelerating, or inflecting — and what the next 1-2 prints likely look like.
- Balance sheet trajectory from the dossier's `annual` block: trend in debt, cash, working capital, capex intensity. Tie capex/working-capital direction to the forward thesis.
- Reference the latest `business_profile` snapshot (today's ROCE, debt/equity, growth metrics) for the live picture.

## 7. Ownership & Insider Signal
- Latest shareholding: promoter %, FII %, DII %, pledge %.
- Direction of change over recent quarters (smart-money accumulating or distributing?).
- Web search for any recent promoter / insider transactions, block / bulk deals, or stake-change disclosures.

## 8. Valuation, Peer Setup & Risks
- Current TTM EPS and P/E.
- Top 3 peer ROC over the recent window — is the stock leading or lagging the pack?
- Web search for current peer multiples; compare and explain the gap.
- "What earnings growth is the current multiple discounting?" Be specific (state the implied 2-yr CAGR).
- 3-5 risks tagged [HIGH] / [MEDIUM] / [LOW]. Specific — concentration, working-capital cycle, regulatory, competitive intensity, leverage, governance.
- Every "Data gap:" you flagged.
- Source URLs from the dossier plus the web pages your grounded claims came from.

```verdict
{
  "stance": "buy | watch | avoid",
  "conviction": "A | B | C",
  "headline": "<= 200 char one-liner capturing the thesis",
  "top_risk": "<= 200 char one-liner naming the single biggest risk to this stance"
}
```
"""


def build_user_prompt(dossier: dict[str, Any], mode: str) -> str:
    template = _RETROSPECTIVE_TEMPLATE if mode == "retrospective" else _FORWARD_TEMPLATE

    identity = dossier.get("identity") or {}
    symbol = identity.get("symbol", "?")
    company = identity.get("company_name", "?")
    window = dossier.get("window") or {}

    header = (
        f"# Research request\n\n"
        f"**Stock:** {company} ({symbol})  \n"
        f"**Mode:** {mode}  \n"
        f"**Window:** {window.get('from')} → {window.get('to')}\n\n"
        f"---\n\n"
    )

    dossier_block = (
        "## Dossier (your only source of truth)\n\n"
        "```json\n"
        f"{json.dumps(dossier, indent=2, default=str)}\n"
        "```\n\n"
        "---\n\n"
    )

    instruction = (
        "## Required output structure\n\n"
        "Produce all 8 sections in the exact order below. Do not change the headings. "
        "Do not add a section 0 or section 9. Do not add a preamble before section 1. "
        "Begin your response with `## 1. Bottom Line`. End section 8 with the "
        "sources list, then append the verdict block, then append the PDF JSON "
        "block exactly as specified at the bottom of this prompt. "
        "Every section header MUST be on its own line, prefixed with `## ` "
        "(e.g. `## 6. Ownership & Insider Signal`) — never inline as `6. Ownership...`.\n\n"
    )

    return (
        header + dossier_block + instruction + template
        + "\n\n---\n\n" + PDF_JSON_SCHEMA
    )


def build_incremental_user_prompt(
    *,
    dossier: dict[str, Any],
    prev_content_md: str,
    prev_created_at: str | None,
    mode: str,
    watermark: str | None,
) -> str:
    """User prompt for an *incremental* refresh.

    The model is given (a) the latest dossier, (b) the previous report's
    full markdown, and (c) the date watermark. Its job is to keep what
    still holds, edit only the sections affected by new evidence, and
    re-emit the whole 8-section structure + verdict block. This costs
    significantly fewer output tokens than a fresh write because most
    paragraphs are copied verbatim.
    """
    template = _RETROSPECTIVE_TEMPLATE if mode == "retrospective" else _FORWARD_TEMPLATE

    identity = dossier.get("identity") or {}
    symbol = identity.get("symbol", "?")
    company = identity.get("company_name", "?")
    window = dossier.get("window") or {}

    header = (
        f"# Research refresh request\n\n"
        f"**Stock:** {company} ({symbol})  \n"
        f"**Mode:** {mode}  \n"
        f"**Window:** {window.get('from')} → {window.get('to')}  \n"
        f"**Previous report written:** {prev_created_at or 'unknown'}  \n"
        f"**Latest evidence watermark:** {watermark or 'unknown'}\n\n"
        f"---\n\n"
    )

    prior = (
        "## Previous report (anchor — keep what still holds)\n\n"
        "Below is the full markdown of the previous research report. Treat it as "
        "your starting draft. Most paragraphs should be copied through unchanged. "
        "Only rewrite the parts that are now stale because of new evidence that "
        "landed after the previous report was written.\n\n"
        "```markdown\n"
        f"{prev_content_md}\n"
        "```\n\n"
        "---\n\n"
    )

    dossier_block = (
        "## Updated dossier (your only source of truth)\n\n"
        "```json\n"
        f"{json.dumps(dossier, indent=2, default=str)}\n"
        "```\n\n"
        "---\n\n"
    )

    instruction = (
        "## What to do\n\n"
        "1. Skim the previous report and the updated dossier. Identify what is "
        "*actually new* since the previous report's date — typically: new "
        "filings, a fresh quarter, ownership changes, recent price action, "
        "and news the model can find via Google Search after that date.\n"
        "2. Re-emit the full 8-section report. **Copy paragraphs from the "
        "previous report verbatim** wherever the underlying facts haven't "
        "changed. This is the default — don't paraphrase for variety.\n"
        "3. Rewrite a section only when the new evidence materially changes "
        "what it should say. When you do, mark the change inline with a "
        "`> **Update (<YYYY-MM-DD>):** …` blockquote at the start of the "
        "affected paragraph so the reader can see what's new at a glance.\n"
        "4. If a fresh quarter has landed since the previous report, the "
        "Fundamentals section MUST reflect it. If a major filing or news "
        "item surfaced, add it to Catalysts/Risks as appropriate.\n"
        "5. Re-emit the verdict block at the end. Update `stance`/`conviction`"
        " only if the new evidence justifies a change — explain the change "
        "in the `headline` field if so.\n"
        "6. Re-emit the PDF JSON block (full schema below) reflecting the "
        "refreshed numbers and any changed pillars / risks / sources. Older "
        "PDFs persisted before the JSON block existed will fall back to "
        "showing 'PDF unavailable' until they're refreshed.\n\n"
        "## Required output structure\n\n"
        "Same as a fresh report — 8 sections in order, then the ```verdict``` "
        "block, then the PDF JSON block. Begin with `## 1. Bottom Line`. "
        "Do not add a preamble explaining that this is an update — just "
        "produce the refreshed report.\n\n"
    )

    return (
        header + prior + dossier_block + instruction + template
        + "\n\n---\n\n" + PDF_JSON_SCHEMA
    )
