from __future__ import annotations

import json

from .actions import cancel_pending_action, confirm_pending_action, get_action_tools, run_action
from .catalog import get_catalog_overview, get_reader_tools, run_reader, suggest_primary_reader, suggest_reader_candidates
from .gateway import AnthropicGateway
from .history import clear_history, get_history, save_message
from .sql_fallback import SQL_FALLBACK_TOOL, execute_sql_fallback, sql_result_text
from .utils import compact_whitespace, money_text, pct_text, to_jsonable


class ValvoAIEngine:
    def __init__(self):
        self.gateway = AnthropicGateway()

    def health(self):
        from database.database import get_db

        checks = {
            "model_gateway": "configured" if self.gateway.available() else "missing_api_key",
            "catalog_readers": len(get_reader_tools()),
            "action_tools": len(get_action_tools()),
        }
        conn = get_db()
        checks["database"] = "connected" if conn else "failed"
        if conn:
            conn.close()
        return checks

    def clear_history(self, page_context: str | None = None):
        return clear_history(page_context)

    def query(self, payload: dict):
        confirm_id = payload.get("confirm_pending_action_id")
        cancel_id = payload.get("cancel_pending_action_id")
        page_context = payload.get("page_context")
        stock_context = payload.get("stock_context")
        mode = payload.get("mode") or payload.get("response_mode") or "auto"
        is_voice = bool(payload.get("voice"))
        model_name = payload.get("model")

        if confirm_id:
            result = confirm_pending_action(confirm_id, request_text=payload.get("message") or "")
            return self._finalize_action_result(result, page_context, stock_context)
        if cancel_id:
            result = cancel_pending_action(cancel_id)
            return self._finalize_action_result(result, page_context, stock_context)

        message = compact_whitespace(payload.get("message") or "")
        if not message:
            return {"error": "message is required"}

        candidate_plan = suggest_reader_candidates(message, page_context=page_context, stock_context=stock_context, limit=4)
        primary = candidate_plan[0] if candidate_plan else suggest_primary_reader(message, page_context=page_context, stock_context=stock_context)
        prefetched = self._prefetch_candidates(candidate_plan, message=message, is_voice=is_voice)
        structured_response = self._select_best_prefetch(prefetched, message)
        tool_results = [item["tool_result"] for item in prefetched]

        if self._should_answer_deterministically(mode, message, structured_response, candidate_plan):
            response_text = self._payload_summary(structured_response, is_voice=is_voice, query_text=message)
            self._save_roundtrip(message, response_text, page_context, stock_context)
            return {
                "response": response_text,
                "structured_response": structured_response,
                "tool_results": tool_results,
                "category": structured_response.get("type") if structured_response else "general",
                "requires_confirmation": False,
                "pending_action": None,
                "format": "quick",
            }

        if not self.gateway.available():
            response_text = self._payload_summary(structured_response, is_voice=is_voice, query_text=message) if structured_response else "Model gateway is not configured."
            self._save_roundtrip(message, response_text, page_context, stock_context)
            return {
                "response": response_text,
                "structured_response": structured_response,
                "tool_results": tool_results,
                "category": structured_response.get("type") if structured_response else "general",
                "requires_confirmation": False,
                "pending_action": None,
                "format": "quick" if structured_response else "detailed",
            }

        system_prompt = self._build_system_prompt(primary, structured_response, prefetched, is_voice=is_voice)
        messages = get_history(limit=10, page_context=page_context) + [{"role": "user", "content": message}]
        tools = get_reader_tools() + get_action_tools() + [SQL_FALLBACK_TOOL]
        response = self.gateway.create_message(
            model=model_name,
            max_tokens=220 if is_voice else 900,
            system=system_prompt,
            messages=messages,
            tools=tools,
        )

        pending_action = None
        rounds = 0
        while response.stop_reason == "tool_use" and rounds < 4:
            rounds += 1
            tool_blocks = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_name = block.name
                tool_input = block.input or {}
                if tool_name.startswith("read_"):
                    data = run_reader(tool_name, tool_input)
                    tool_results.append(
                        {
                            "tool": tool_name,
                            "kind": "reader",
                            "result": self._payload_summary(data, is_voice=is_voice, query_text=message),
                            "data": data,
                        }
                    )
                    if self._should_replace_structured_response(structured_response, data, query_text=message):
                        structured_response = data
                    content = json.dumps(to_jsonable(data), ensure_ascii=True)
                elif tool_name == "sql_read_fallback":
                    data = execute_sql_fallback(tool_input.get("sql_query") or "")
                    tool_results.append(
                        {
                            "tool": tool_name,
                            "kind": "sql",
                            "result": sql_result_text(data),
                            "data": data,
                        }
                    )
                    content = json.dumps(to_jsonable(data), ensure_ascii=True)
                else:
                    data = run_action(tool_name, tool_input, request_text=message)
                    tool_results.append(
                        {
                            "tool": tool_name,
                            "kind": "action",
                            "result": data.get("message") or data.get("error") or tool_name,
                            "data": data,
                        }
                    )
                    if data.get("requires_confirmation"):
                        pending_action = data.get("pending_action")
                    content = json.dumps(to_jsonable(data), ensure_ascii=True)
                tool_blocks.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_blocks},
            ]
            response = self.gateway.create_message(
                model=model_name,
                max_tokens=180 if is_voice else 800,
                system=system_prompt,
                messages=messages,
                tools=tools,
            )

        response_text = ""
        for block in response.content:
            if hasattr(block, "text") and block.text:
                response_text += block.text
        response_text = compact_whitespace(response_text) or self._payload_summary(structured_response, is_voice=is_voice, query_text=message)

        self._save_roundtrip(message, response_text, page_context, stock_context)

        return {
            "response": response_text,
            "structured_response": structured_response,
            "tool_results": tool_results,
            "category": structured_response.get("type") if structured_response else (pending_action.get("action_name") if pending_action else "general"),
            "requires_confirmation": bool(pending_action),
            "pending_action": pending_action,
            "input_tokens": getattr(response.usage, "input_tokens", None),
            "output_tokens": getattr(response.usage, "output_tokens", None),
            "format": "quick" if structured_response else "detailed",
        }

    def _save_roundtrip(self, user_message: str, assistant_message: str, page_context: str | None, stock_context: str | None):
        save_message("user", user_message, page_context=page_context, stock_context=stock_context)
        save_message("assistant", assistant_message, page_context=page_context, stock_context=stock_context)

    def _prefetch_candidates(self, candidates: list[dict], message: str, is_voice: bool):
        if not candidates:
            return []
        prefetched = []
        top_score = candidates[0]["score"]
        max_prefetch = 2 if is_voice else 3
        for candidate in candidates:
            if len(prefetched) >= max_prefetch:
                break
            if candidate["score"] < 70 and prefetched:
                continue
            if candidate["score"] < top_score - 22 and prefetched:
                continue
            data = run_reader(candidate["name"], candidate.get("args"))
            prefetched.append(
                {
                    "candidate": candidate,
                    "data": data,
                    "tool_result": {
                        "tool": candidate["name"],
                        "kind": "reader",
                        "domain": candidate["name"].replace("read_", ""),
                        "reason": candidate.get("reason"),
                        "score": candidate.get("score"),
                        "result": self._payload_summary(data, is_voice=is_voice, query_text=message),
                        "data": data,
                    },
                }
            )
        return prefetched

    def _payload_match_score(self, payload: dict | None, query_text: str | None = None):
        if not payload:
            return -100
        ptype = payload.get("type")
        if ptype == "error":
            return -90
        if ptype == "empty":
            return -20
        lowered = (query_text or "").lower()
        score = 25
        if ptype == "positions" and any(token in lowered for token in ["portfolio", "positions", "overview", "holdings"]):
            score += 60
        if ptype == "actions" and any(token in lowered for token in ["sell", "trim", "exit"]):
            score += 70
        if ptype == "risk" and any(token in lowered for token in ["risk", "exposure", "downside"]):
            score += 70
        if ptype == "rankings" and any(token in lowered for token in ["rank", "ranking", "top performer", "worst performer"]):
            score += 70
        if ptype == "trailing" and any(token in lowered for token in ["trail", "5ma", "defensive"]):
            score += 70
        if ptype == "single_stock":
            score += 55
        if ptype == "journal_stats" and any(token in lowered for token in ["win rate", "track record", "trade history", "historical performance"]):
            score += 65
        if ptype == "trade_stats_period" and any(token in lowered for token in ["month", "year", "recent", "rolling", "period"]):
            score += 78
        if ptype == "journal_trade_book" and any(token in lowered for token in ["journal trades", "open journal trades", "closed journal trades", "editable trades"]):
            score += 82
        if ptype == "journal_settings_summary" and any(token in lowered for token in ["journal settings", "fund months", "portfolio capital", "tax rate"]):
            score += 82
        if ptype == "streak_analysis" and any(token in lowered for token in ["streak", "consecutive", "after 2 losses", "after 3 wins"]):
            score += 85
        if ptype == "trade_highlights" and any(token in lowered for token in ["best trade", "biggest trade", "largest trade", "last 3 years", "fy"]):
            score += 82
        if ptype == "trade_r_extremes" and any(token in lowered for token in ["r multiple", "r-multiple", "biggest r trade", "highest r trade", "best r trade", "largest r trade"]):
            score += 90
        if ptype == "trade_winners" and any(token in lowered for token in ["top winners", "biggest winners", "winning past trades", "past winners", "% winners", "percent winners", "top winning trades"]):
            score += 92
        if ptype == "trade_history" and any(token in lowered for token in ["recent trades", "trade history", "history preview", "fy trades"]):
            score += 80
        if ptype == "analytics_fy" and "fy" in lowered:
            score += 70
        if ptype == "analytics_overview" and any(token in lowered for token in ["expectancy", "profit factor", "payoff ratio", "average winner", "avg winner", "average loser", "avg loser", "best trade", "worst trade", "return"]):
            score += 85
        if ptype == "drawdown_analysis" and any(token in lowered for token in ["drawdown", "recovery", "negative month", "underperform", "outperform"]):
            score += 88
        if ptype == "equity_curve" and any(token in lowered for token in ["equity curve", "cumulative growth", "capital added", "long term equity"]):
            score += 88
        if ptype == "outlier_analysis" and any(token in lowered for token in ["outlier", "distribution", "concentration", "fat tail", "top 5 winners", "without top 3"]):
            score += 88
        if ptype == "analytics_monthly" and any(token in lowered for token in ["monthly", "month", "best month", "worst month"]):
            score += 78
        if ptype == "regime_summary" and any(token in lowered for token in ["regime", "leading sectors"]):
            score += 75
        if ptype == "sector_snapshot" and any(token in lowered for token in ["sector", "breadth", "rotation"]):
            score += 75
        if ptype == "index_constituents_snapshot" and any(token in lowered for token in ["constituents", "members", "stocks in", "sector members", "index members"]):
            score += 84
        if ptype == "scoring_snapshot" and "scoring" in lowered:
            score += 72
        if ptype == "screener_snapshot" and any(token in lowered for token in ["screener", "scan", "movers"]):
            score += 72
        if ptype == "saved_scanners_snapshot" and "saved scanner" in lowered:
            score += 72
        if ptype == "live_monitor" and any(token in lowered for token in ["live monitor", "live positions", "real time positions", "current price now"]):
            score += 84
        if ptype == "explore_stock":
            score += 68
            if any(token in lowered for token in ["52 week", "52w", "adr", "liquidity", "market cap", "industry", "relative strength", "range position", "from high", "from low", "ma20", "ma50", "ma200", "ma 20", "ma 50", "ma 200"]):
                score += 22
        if ptype == "explore_insights" and any(token in lowered for token in ["stock insights", "explore insights", "recent results", "sectoral tailwind", "risk factors"]):
            score += 82
        return score

    def _select_best_prefetch(self, prefetched: list[dict], query_text: str):
        best_payload = None
        best_score = -1000
        for item in prefetched:
            payload = item["data"]
            candidate_score = int(item["candidate"].get("score") or 0)
            total_score = candidate_score + self._payload_match_score(payload, query_text=query_text)
            if total_score > best_score:
                best_payload = payload
                best_score = total_score
        return best_payload

    def _should_answer_deterministically(self, mode: str, message: str, payload: dict | None, candidate_plan: list[dict] | None = None):
        if not payload:
            return False
        if payload.get("type") == "error":
            return False
        if payload.get("type") == "empty":
            return True
        if payload.get("type") in {"trade_highlights", "streak_analysis", "trade_winners"}:
            return True
        lowered = message.lower()
        top_score = (candidate_plan or [{}])[0].get("score", 0) if candidate_plan else 0
        direct_types = {"positions", "actions", "risk", "rankings", "trailing", "single_stock", "regime_summary", "sector_snapshot", "index_constituents_snapshot", "saved_scanners_snapshot", "screener_snapshot", "scoring_snapshot", "analytics_monthly", "trade_stats_period", "trade_history", "journal_trade_book", "journal_settings_summary", "live_monitor", "equity_curve", "explore_stock", "explore_insights", "trade_r_extremes", "trade_winners"}
        analytics_metric_tokens = ["expectancy", "profit factor", "payoff ratio", "average winner", "avg winner", "average loser", "avg loser", "best trade", "worst trade", "best month", "worst month"]
        stock_metric_tokens = ["52 week", "52w", "adr", "liquidity", "market cap", "industry", "relative strength", "range position", "from high", "from low", "ma20", "ma50", "ma200", "ma 20", "ma 50", "ma 200"]
        if payload.get("type") in direct_types and top_score >= 90 and not self._looks_complex(lowered):
            return True
        if payload.get("type") == "analytics_overview" and top_score >= 110 and any(token in lowered for token in analytics_metric_tokens):
            return True
        if payload.get("type") == "explore_stock" and top_score >= 100 and any(token in lowered for token in stock_metric_tokens):
            return True
        if mode == "quick" and payload.get("type") in direct_types and top_score >= 108:
            return True
        if mode == "auto" and self._looks_complex(lowered):
            return False
        if payload.get("type") in {"analytics_overview", "drawdown_analysis", "outlier_analysis"}:
            return False
        if any(token in lowered for token in ["overview", "sell", "risk", "rank", "5ma", "watchlist", "regime"]) and top_score >= 95:
            return True
        return False

    def _looks_complex(self, lowered_message: str):
        complexity_signals = [
            " and ",
            "compare",
            " vs ",
            "versus",
            "last 3 years",
            "last 2 years",
            "past 3 years",
            "past 2 years",
            "which",
            "across",
            "fy",
        ]
        return any(signal in lowered_message for signal in complexity_signals)

    def _build_system_prompt(self, primary_reader: dict | None, structured_response: dict | None, prefetched_items: list[dict], is_voice: bool = False):
        catalog_overview = json.dumps(get_catalog_overview(), ensure_ascii=True)
        prefetched_context = json.dumps(to_jsonable(structured_response), ensure_ascii=True)[:3000] if structured_response else "null"
        prefetched_candidates = json.dumps(
            [
                {
                    "tool": item["candidate"]["name"],
                    "score": item["candidate"].get("score"),
                    "reason": item["candidate"].get("reason"),
                    "summary": item["tool_result"]["result"],
                    "type": item["data"].get("type"),
                }
                for item in prefetched_items
            ],
            ensure_ascii=True,
        )[:3000]
        voice_rules = ""
        if is_voice:
            voice_rules = "\nVoice mode: answer in at most 2 short sentences and keep numbers crisp."
        return (
            "You are Valvo AI v2, a data-first trading intelligence system.\n"
            "Rules:\n"
            "1. Never fabricate numbers. Use typed read tools first.\n"
            "2. SQL fallback is the last resort and read-only.\n"
            "3. Never suggest or attempt direct writes outside named action tools.\n"
            "4. Legacy analytics tables are read-only and must stay untouched.\n"
            "5. Return plain helpful text only. The backend owns structured UI payloads.\n"
            f"Catalog overview: {catalog_overview}\n"
            f"Prefetched context: {prefetched_context}\n"
            f"Prefetched reader candidates: {prefetched_candidates}\n"
            f"Primary reader hint: {primary_reader['name'] if primary_reader else 'none'}."
            f"{voice_rules}"
        )

    def _should_replace_structured_response(self, current_payload: dict | None, new_payload: dict | None, query_text: str):
        if not new_payload:
            return False
        if not current_payload:
            return new_payload.get("type") not in {"error"}
        return self._payload_match_score(new_payload, query_text=query_text) > self._payload_match_score(current_payload, query_text=query_text)

    def _finalize_action_result(self, result: dict, page_context: str | None, stock_context: str | None):
        response_text = result.get("message") or result.get("error") or "Action processed."
        self._save_roundtrip(response_text if result.get("cancelled") else "Action confirmation", response_text, page_context, stock_context)
        return {
            "response": response_text,
            "structured_response": None,
            "tool_results": [{"tool": result.get("action_name") or "pending_action", "kind": "action", "result": response_text, "data": result}],
            "category": "action",
            "requires_confirmation": False,
            "pending_action": None,
            "format": "quick",
        }

    def _payload_summary(self, payload: dict | None, is_voice: bool = False, query_text: str | None = None):
        if not payload:
            return "No data available."
        ptype = payload.get("type")
        if ptype == "empty":
            return payload.get("message") or "No matching data."
        if ptype == "positions":
            summary = payload.get("summary") or {}
            cards = payload.get("cards") or []
            if not cards:
                return "No active positions."
            best = max(cards, key=lambda item: item.get("r", 0))
            weakest = min(cards, key=lambda item: item.get("r", 0))
            return (
                f"{summary.get('count', len(cards))} active positions, total P&L {money_text(summary.get('total_pnl'))}. "
                f"Best is {best['name']} at {best['r']:+.1f}R. Weakest is {weakest['name']} at {weakest['r']:+.1f}R."
            )
        if ptype == "actions":
            cards = payload.get("cards") or []
            if not cards:
                return "No sell actions are active."
            top = cards[0]
            return f"Top action is {top['name']}: {top['verdict']}. Current extension is {pct_text(top.get('ext'), 0)} and R multiple is {top.get('r', 0):+.1f}."
        if ptype == "risk":
            cards = payload.get("cards") or []
            top = cards[0] if cards else None
            return (
                f"Defined downside is {money_text(payload.get('total_risk'))}. "
                f"Highest risk is {top['name']} at {money_text(top['risk_rupees'])}." if top else "Risk view is ready."
            )
        if ptype == "rankings":
            cards = payload.get("cards") or []
            top = cards[:3]
            return " | ".join(f"#{item['rank']} {item['name']} {item['r']:+.1f}R" for item in top) if top else "No rankings available."
        if ptype == "trailing":
            return f"{payload.get('alerts', 0)} of {payload.get('total', 0)} positions need 5MA attention."
        if ptype == "single_stock":
            card = payload.get("card") or {}
            return f"{card.get('name')} is at {card.get('cmp')} versus entry {card.get('entry')}, with {card.get('r', 0):+.1f}R and stop {card.get('sl')}."
        if ptype == "journal_stats":
            stats = payload.get("stats") or {}
            return f"Historical win rate is {stats.get('win_rate', 0)}% across {stats.get('total_trades', 0)} trades, with total P&L {money_text(stats.get('total_pl'))}."
        if ptype == "trade_stats_period":
            period = payload.get("period", "1y")
            return (
                f"Over the {period}, win rate is {payload.get('win_rate', 0)}% across {payload.get('total_trades', 0)} trades. "
                f"Average winner is {pct_text(payload.get('avg_winner_pct'), 2)} and average loser is {pct_text(payload.get('avg_loser_pct'), 2)}."
            )
        if ptype == "journal_trade_book":
            cards = payload.get("cards") or []
            top = cards[0] if cards else None
            if top:
                return (
                    f"The journal has {payload.get('count', 0)} {payload.get('status', 'all')} trades. "
                    f"Latest is {top.get('symbol')} with {money_text(top.get('gross_pl'))} and {top.get('reward_risk', 0)}R."
                )
            return "No journal trades found."
        if ptype == "journal_settings_summary":
            settings = payload.get("settings") or {}
            summary = payload.get("summary") or {}
            return (
                f"Journal portfolio capital is {money_text(settings.get('portfolio_capital'))}. "
                f"Net fund flow for {payload.get('year')} is {money_text(summary.get('net_added'))}."
            )
        if ptype == "streak_analysis":
            worst = payload.get("worst_streak") or {}
            best = payload.get("best_streak") or {}
            current = payload.get("current") or {}
            current_prefix = current.get("type") or "N/A"
            lowered_query = (query_text or "").lower()
            if "after 2 losses" in lowered_query:
                sample = payload.get("after_2_losses") or {}
                win_pct = sample.get("next_win_pct")
                if win_pct is None:
                    return "There is not enough history after 2-loss sequences yet."
                return f"After 2 consecutive losses, the next trade won {win_pct}% of the time across {sample.get('sample_size', 0)} samples."
            if "after 3 wins" in lowered_query:
                sample = payload.get("after_3_wins") or {}
                win_pct = sample.get("next_win_pct")
                if win_pct is None:
                    return "There is not enough history after 3-win sequences yet."
                return f"After 3 consecutive wins, the next trade also won {win_pct}% of the time across {sample.get('sample_size', 0)} samples."
            if "winning" in lowered_query and "losing" not in lowered_query:
                return f"Your longest winning streak is {best.get('len', 0)} consecutive trades. The current streak is {current_prefix}{current.get('len', 0)}."
            if "losing" in lowered_query and "winning" not in lowered_query:
                return f"Your longest losing streak is {worst.get('len', 0)} consecutive trades. The current streak is {current_prefix}{current.get('len', 0)}."
            return (
                f"Your longest losing streak is {worst.get('len', 0)} consecutive trades. "
                f"Longest winning streak is {best.get('len', 0)}, and the current streak is {current_prefix}{current.get('len', 0)}."
            )
        if ptype == "trade_highlights":
            best = payload.get("best_trade_last_years") or {}
            biggest = payload.get("biggest_trade_fy") or {}
            if best and biggest:
                biggest_buy_value = money_text(biggest.get("buy_value")).lstrip("+")
                return (
                    f"Best trade across the last {payload.get('years', 3)} years was {best.get('symbol')} in {best.get('fy')}, "
                    f"delivering {money_text(best.get('realized_pl'))} ({pct_text(best.get('realized_pl_pct'), 2)}). "
                    f"The biggest trade in {payload.get('target_fy')} was {biggest.get('symbol')} with buy value {biggest_buy_value}."
                )
            return "Trade highlights are ready."
        if ptype == "trade_r_extremes":
            best = payload.get("best_r_trade") or {}
            worst = payload.get("worst_r_trade") or {}
            lowered_query = (query_text or "").lower()
            if any(token in lowered_query for token in ["worst r", "lowest r", "most negative r"]):
                return (
                    f"Lowest R trade {('in ' + payload.get('fy')) if payload.get('fy') and payload.get('fy') != 'all' else 'across the dataset'} "
                    f"was {worst.get('symbol')} at {worst.get('r_multiple')}R, with P&L {money_text(worst.get('realized_pl'))} "
                    f"and move {pct_text(worst.get('realized_pl_pct'), 2)}."
                ) if worst else "No R-multiple history available."
            return (
                f"Highest R trade {('in ' + payload.get('fy')) if payload.get('fy') and payload.get('fy') != 'all' else 'across the whole dataset'} "
                f"was {best.get('symbol')} at {best.get('r_multiple')}R, with P&L {money_text(best.get('realized_pl'))} "
                f"and move {pct_text(best.get('realized_pl_pct'), 2)}."
            ) if best else "No R-multiple history available."
        if ptype == "trade_winners":
            cards = payload.get("cards") or []
            if not cards:
                return "No winning trade history available."
            label = "percentage winners" if payload.get("sort_by") == "pct" else "winning trades by P&L"
            lines = []
            for index, card in enumerate(cards[: payload.get("count", len(cards))], start=1):
                if payload.get("sort_by") == "pct":
                    metric = pct_text(card.get("realized_pl_pct"), 2)
                    detail = money_text(card.get("realized_pl"))
                else:
                    metric = money_text(card.get("realized_pl"))
                    detail = pct_text(card.get("realized_pl_pct"), 2)
                lines.append(f"{index}. {card.get('symbol')} {metric} ({detail})")
            scope = f"in {payload.get('fy')}" if payload.get("fy") and payload.get("fy") != "all" else "across your full history"
            return f"Top {len(lines)} {label} {scope}: " + " | ".join(lines)
        if ptype == "analytics_fy":
            summary = payload.get("summary") or {}
            return f"{payload.get('fy')} delivered {money_text(summary.get('total_pl'))} across {summary.get('total_trades', 0)} trades at {summary.get('win_rate', 0)}% win rate."
        if ptype == "analytics_overview":
            summary = payload.get("summary") or {}
            lowered_query = (query_text or "").lower()
            best_trade = payload.get("best_trade") or {}
            worst_trade = payload.get("worst_trade") or {}
            best_month = payload.get("best_month") or {}
            worst_month = payload.get("worst_month") or {}
            if "expectancy" in lowered_query or "profit factor" in lowered_query or "payoff ratio" in lowered_query:
                return (
                    f"Expectancy is {summary.get('expectancy_r', 0)}R, profit factor is {summary.get('profit_factor', 0)}, "
                    f"and payoff ratio is {summary.get('payoff_ratio', 0)}. Win rate is {summary.get('win_rate', 0)}%."
                )
            if "average winner" in lowered_query or "avg winner" in lowered_query or "average loser" in lowered_query or "avg loser" in lowered_query:
                return (
                    f"Average winner is {pct_text(summary.get('avg_winner_pct'), 2)} and average loser is {pct_text(summary.get('avg_loser_pct'), 2)}. "
                    f"Average winner R is {summary.get('avg_winner_r', 0)} and average loser R is {summary.get('avg_loser_r', 0)}."
                )
            if "best trade" in lowered_query or "worst trade" in lowered_query:
                return (
                    f"Best trade was {best_trade.get('symbol')} for {money_text(best_trade.get('realized_pl'))}. "
                    f"Worst trade was {worst_trade.get('symbol')} for {money_text(worst_trade.get('realized_pl'))}."
                )
            if "best month" in lowered_query or "worst month" in lowered_query:
                return (
                    f"Best month was {best_month.get('month_label')} at {pct_text(best_month.get('after_charges'), 2)}. "
                    f"Worst month was {worst_month.get('month_label')} at {pct_text(worst_month.get('after_charges'), 2)}."
                )
            return (
                f"{payload.get('fy', 'All history')} has {summary.get('total_trades', 0)} trades with {summary.get('win_rate', 0)}% win rate "
                f"and total P&L {money_text(summary.get('total_pl'))}. Expectancy is {summary.get('expectancy_r', 0)}R and profit factor is {summary.get('profit_factor', 0)}."
            )
        if ptype == "trade_history":
            cards = payload.get("cards") or []
            latest = cards[0] if cards else None
            return (
                f"{payload.get('fy')} trade history has {payload.get('count', 0)} trades with total P&L {money_text(payload.get('total_pl'))}. "
                f"Latest trade in the preview is {latest.get('symbol')} for {money_text(latest.get('realized_pl'))}."
                if latest
                else "No trade history available."
            )
        if ptype == "regime_summary":
            return f"Current regime is {payload.get('regime')}. Leading sectors: {', '.join(payload.get('leading_sectors') or []) or 'none'}."
        if ptype == "sector_snapshot":
            cards = payload.get("cards") or []
            top = cards[0] if cards else None
            return f"Top sector over {payload.get('days', 20)} sessions is {top['symbol']} at {top['pct_change']}%." if top else "No sector data available."
        if ptype == "index_constituents_snapshot":
            summary = payload.get("summary") or {}
            leaders = payload.get("leaders") or []
            lead = leaders[0] if leaders else None
            return (
                f"{payload.get('index_symbol')} has {payload.get('count', 0)} tracked constituents. "
                f"{summary.get('uptrend_count', 0)} are in uptrend, {summary.get('mixed_count', 0)} are mixed, and {summary.get('downtrend_count', 0)} are in downtrend. "
                f"Top weekly mover is {lead.get('stock_symbol')} at {pct_text(lead.get('week_change'), 2)}."
            ) if lead else f"{payload.get('index_symbol')} constituent breadth is ready."
        if ptype == "live_monitor":
            cards = payload.get("cards") or []
            hottest = max(cards, key=lambda item: float(item.get("r_multiple") or 0)) if cards else None
            return f"Live monitor is tracking {payload.get('count', 0)} positions. Best live R is {hottest.get('stock_name')} at {hottest.get('r_multiple')}R." if hottest else "No live monitor data available."
        if ptype == "scoring_snapshot":
            cards = payload.get("cards") or []
            top = cards[0] if cards else None
            return f"Latest scoring book has {payload.get('count', 0)} entries. Best recent score: {top['symbol']} at {top['final_score']}." if top else "No scoring data available."
        if ptype == "screener_snapshot":
            cards = payload.get("cards") or []
            top = cards[0] if cards else None
            return f"Latest screener snapshot is from {payload.get('scan_date')}. Top mover is {top['symbol']} at {top['pchange']}%." if top else "No screener snapshot available."
        if ptype == "analytics_monthly":
            months = payload.get("months") or []
            best = max(months, key=lambda item: float(item.get("after_charges") or 0)) if months else None
            worst = min(months, key=lambda item: float(item.get("after_charges") or 0)) if months else None
            lowered_query = (query_text or "").lower()
            if "worst month" in lowered_query and worst:
                return f"Worst month in {payload.get('fy')} was {worst['month_label']} at {pct_text(worst.get('after_charges'), 2)}."
            return f"{payload.get('fy')} has {len(months)} monthly records. Best month was {best['month_label']} at {pct_text(best.get('after_charges'), 2)}." if best else "No monthly analytics available."
        if ptype == "drawdown_analysis":
            summary = payload.get("summary") or {}
            lowered_query = (query_text or "").lower()
            if "recovery" in lowered_query:
                return (
                    f"Average recovery after a drawdown is {summary.get('avg_recovery_months', 0)} months across {summary.get('dd_periods_count', 0)} drawdown periods. "
                    f"The worst drawdown was {summary.get('max_dd_pct', 0)}% in {summary.get('max_dd_month')}."
                )
            if "underperform" in lowered_query or "outperform" in lowered_query:
                return (
                    f"You underperformed in {summary.get('underperformance_months', 0)} months and outperformed in {summary.get('outperformance_months', 0)} down-market months. "
                    f"Maximum drawdown was {summary.get('max_dd_pct', 0)}%."
                )
            return (
                f"Maximum drawdown was {summary.get('max_dd_pct', 0)}% in {summary.get('max_dd_month')}, "
                f"with a longest negative-month streak of {summary.get('max_consecutive_negative', 0)} and average recovery of {summary.get('avg_recovery_months', 0)} months."
            )
        if ptype == "equity_curve":
            fy_summaries = payload.get("fy_summaries") or []
            latest = fy_summaries[-1] if fy_summaries else {}
            return (
                f"Long-term equity curve stands at {money_text(payload.get('final_amount'))} with cumulative growth of {payload.get('final_cumm_pct', 0)}%. "
                f"Latest FY tracked is {latest.get('fy')} at {pct_text(latest.get('net_return_pct'), 2)}."
            )
        if ptype == "outlier_analysis":
            summary = payload.get("summary") or {}
            top = (payload.get("top_winners") or [{}])[0]
            return (
                f"{payload.get('fy')} has {summary.get('outlier_5p', 0)} winners above 5% and {summary.get('outlier_2r', 0)} winners above 2R. "
                f"Top winner was {top.get('symbol')} for {money_text(top.get('pl'))}."
            )
        if ptype == "saved_scanners_snapshot":
            return f"You have {payload.get('count', 0)} saved scanner presets."
        if ptype == "explore_stock":
            lowered_query = (query_text or "").lower()
            symbol = payload.get("symbol")
            cmp_value = payload.get("cmp")
            if any(token in lowered_query for token in ["52 week", "52w", "from high", "from low", "range position"]):
                return (
                    f"{symbol} is at {cmp_value}, {pct_text(payload.get('pct_from_high'), 1)} from the 52-week high and "
                    f"{pct_text(payload.get('pct_from_low'), 1)} from the 52-week low. Range position is {pct_text(payload.get('range_position'), 1)}."
                )
            if any(token in lowered_query for token in ["adr", "liquidity"]):
                return (
                    f"{symbol} has ADR {pct_text(payload.get('adr'), 2)} and 20-day liquidity of {money_text((payload.get('liquidity_cr') or 0) * 10000000)} "
                    f"per day ({payload.get('liquidity_verdict')})."
                )
            if any(token in lowered_query for token in ["market cap", "industry", "sector"]):
                sector = payload.get("sector") or "unknown"
                industry = payload.get("industry") or "not cached"
                return (
                    f"{symbol} sits in {sector}. Market cap proxy is {payload.get('market_cap_label')} at about {money_text((payload.get('market_cap_cr') or 0) * 10000000)}, "
                    f"and industry is {industry}."
                )
            if any(token in lowered_query for token in ["relative strength", "rs", "strength"]):
                rs = payload.get("rs") or {}
                return (
                    f"{symbol} relative strength vs Smallcap 100 is {pct_text(rs.get('rs_1w'), 2)} over 1 week, "
                    f"{pct_text(rs.get('rs_3m'), 2)} over 3 months, and {pct_text(rs.get('rs_6m'), 2)} over 6 months."
                )
            if any(token in lowered_query for token in ["ma20", "ma50", "ma200", "moving average", "ma "]):
                return (
                    f"{symbol} is {payload.get('mas_above_count', 0)}/5 above key moving averages, which is a {payload.get('ma_verdict')} structure. "
                    f"MA20 is {payload.get('mas', {}).get('ma20', {}).get('value')}, MA50 is {payload.get('mas', {}).get('ma50', {}).get('value')}, "
                    f"and MA200 is {payload.get('mas', {}).get('ma200', {}).get('value')}."
                )
            return (
                f"{symbol} is at {cmp_value} with {pct_text(payload.get('day_change'), 2)} on the day. "
                f"It is {payload.get('mas_above_count', 0)}/5 above key moving averages, ADR is {pct_text(payload.get('adr'), 2)}, "
                f"and liquidity is {money_text((payload.get('liquidity_cr') or 0) * 10000000)} per day."
            )
        if ptype == "explore_insights":
            insights = payload.get("insights") or {}
            recent = insights.get("recent_results") if isinstance(insights, dict) else None
            return recent or f"Cached explore insights are available for {payload.get('symbol')}."
        return "Data loaded."
