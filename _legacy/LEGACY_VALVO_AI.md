# Legacy Valvo AI versions — archive

Retired **2026-05-10** in favor of **V7 (DeepSeek)**, which is now the only Valvo AI chat engine.

This folder preserves every previous chat-engine version verbatim so any of them can be revived later (rollback, A/B test, providing different reasoning capabilities, etc.) without digging through git history.

---

## Inventory

| Version | Provider / model | What it added | Original paths | Archived path |
|---|---|---|---|---|
| **v2** | Anthropic / Claude Sonnet 4.6 | First production chat with tools, pending-action confirmation flow, audit log. | `Backend/services/valvo_ai_v2/` *(stays active — see "v2 caveat" below)* + `Backend/routes/valvo_ai_v2_routes.py` | Route file: `_legacy/routes/valvo_ai_v2_routes.py` |
| **v3** | Google / `gemini-2.5-flash` | Gemini-only blueprint, 30× cheaper than v2. Single provider, no fallback. SSE streaming + grouped history-list. | `Backend/services/valvo_ai_v3/` + `Backend/routes/valvo_ai_v3_routes.py` | `_legacy/services/valvo_ai_v3/` + `_legacy/routes/valvo_ai_v3_routes.py` |
| **v4** | Google / dual-model router (`flash` ⇄ `flash-lite`) | Semantic complexity router picks Flash for tool use, Flash-Lite for simple chat. Voice-mode `query()` (300 tok max). Reindex-stocks cron endpoint. | `Backend/services/valvo_ai_v4/` + `Backend/routes/valvo_ai_v4_routes.py` | `_legacy/services/valvo_ai_v4/` + `_legacy/routes/valvo_ai_v4_routes.py` |
| **v5** | Google / `gemini-2.5-flash` (default) + lite fallback | Was the main-page default for ~6 months. Added proactive `/opener`, user `/memory` (GET / POST refresh / DELETE), `/lessons` graduation flow, `portfolio_oracle`. | `Backend/services/valvo_ai_v5/` + `Backend/routes/valvo_ai_v5_routes.py` | `_legacy/services/valvo_ai_v5/` + `_legacy/routes/valvo_ai_v5_routes.py` |
| **v6** | Moonshot / `kimi-k2.6` (default) + Gemini + Anthropic fallback | First multi-provider blueprint. 262K context, OpenAI-compatible function calling, three-way fallback chain. | `Backend/services/valvo_ai_v6/` + `Backend/routes/valvo_ai_v6_routes.py` | `_legacy/services/valvo_ai_v6/` + `_legacy/routes/valvo_ai_v6_routes.py` |

Also archived: `_legacy/app_valvo_ai.py` (the old standalone Flask runner — only ever booted v2 on port 8081, used during early local dev).

Test files that exclusively cover legacy code: `_legacy/tests/test_llm_gateway.py` (v3 + v6 gateway tests) and `_legacy/tests/test_portfolio_oracle.py` (v5 oracle).

---

## v2 caveat — stays active

`Backend/services/valvo_ai_v2/` itself is **not moved**. It continues to live at the active path because three modules inside it became shared utilities relied on by V7 and other blueprints:

- `services.valvo_ai_v2.actions` — pending-action confirm/cancel (`confirm_pending_action`, `cancel_pending_action`). Imported by `routes/valvo_ai_v7_routes.py` and `services/valvo_ai_v7/tools.py`.
- `services.valvo_ai_v2.utils` — JSON serialization helpers (`to_jsonable`). Imported by `services/valvo_ai_v7/tools.py`.
- `services.valvo_ai_v2.catalog` — stock-reference resolver (`_resolve_stock_reference`, `resolve_stock_reference_strict`). Imported by `routes/position_routes.py`.

Only the v2 route file (`valvo_ai_v2_routes.py`) is archived — the blueprint is no longer registered. The DB-init script (`Backend/database/init_valvo_ai_v2_db.py`) also remains active because the tables it creates (`valvo_ai_v2_pending_actions`, `valvo_ai_v2_audit_log`) are used by V7's pending-action flow.

If V7 is ever rewritten to drop these dependencies, the whole `valvo_ai_v2/` folder can move into `_legacy/services/`.

---

## Dropped V5 features

Three V5-only features were intentionally dropped during the V7 consolidation (user decision, 2026-05-10):

1. **Proactive opener** — `GET /api/valvo-ai-v5/opener` returned a context-aware greeting that appeared on the empty state of `/valvo-ai`. Frontend code paths in `ValvoAIRebuildPage.jsx` (`OPENER_CACHE_KEY`, `apiFetch(.../opener)`, `openerLoading` gates) were removed.
2. **User memory** — `GET / POST /memory/refresh / DELETE /api/valvo-ai-v5/memory`. Stored personalized memory snippets used to bias the system prompt. The DB rows survive — V5 reads them; V7 ignores them. Reviving V5 brings them back unchanged.
3. **Lessons graduation** — `/api/valvo-ai-v5/lessons{,/staged}` and `POST .../<id>/graduate`. Curation flow that moved staged "lesson" rows into the canonical user memory.

Source code for all three lives in `_legacy/services/valvo_ai_v5/` and `_legacy/routes/valvo_ai_v5_routes.py`.

---

## Revival checklist

To bring any version back online:

1. **Move the service folder back**: `git mv Backend/_legacy/services/valvo_ai_vX Backend/services/valvo_ai_vX`
2. **Move the route file back**: `git mv Backend/_legacy/routes/valvo_ai_vX_routes.py Backend/routes/valvo_ai_vX_routes.py`
3. **Re-add the import + register_blueprint** in `Backend/app.py` (currently only v7 is registered around lines 36 + 186).
4. **Frontend** — re-add the version option in `Frontend/src/components/valvo-ai-v2/ValvoAIRebuildPage.jsx`'s `ValvoVersionSwitcher` and (optionally) the `Frontend/src/components/SettingsPage.jsx` segmented control. Update `Frontend/src/context/SettingsContext.jsx` if you want it to be the new default (bump the migration key, e.g. `valvo_ai_engine_v8_migrated`).
5. **For v4 specifically** — re-add `"valvo_ai_v4.reindex_stocks"` to the `AUTH_EXEMPT_ENDPOINTS` set in `Backend/app.py`, and re-enable the Cloud Scheduler job that hits that endpoint.
6. **For v5 specifically** — if reviving for the memory/lessons features, no DB migration needed; the `valvo_ai_v5_*` tables in Supabase weren't dropped.

---

## Cloud Scheduler — manual cleanup needed

V4 had a `/api/valvo-ai-v4/reindex-stocks` endpoint hit by a Cloud Scheduler job. After V7 deploy, that job will return **404** every run. **Disable or delete it** in the GCP console:

```
gcloud scheduler jobs list --project valvo-backend --location asia-south1
gcloud scheduler jobs pause <job-name> --project valvo-backend --location asia-south1
```

(or do it via the console — the job name will reference `reindex-stocks`).
