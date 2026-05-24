# Valvo AI regression harness

Offline tests that freeze the shapes the LLM depends on. No DB, no API
keys, no LLM calls — just pure logic over the `actions.py` module with
a stubbed cursor.

## Run

From the repo root:

```bash
python3 -m unittest discover -s Backend/tests -p "test_*.py" -v
```

## What's here

- `_harness.py` — stubs Flask/psycopg2 + a MockCursor. Each test composes
  its own canned row set, so no two tests share state.
- `test_action_prevalidation.py` — every `_prevalidate_action` failure
  path that a user has hit (stock_already_held, no_active_position,
  ambiguous_stock_reference). Each asserts `error_code`, `suggested_action`,
  and `suggested_payload` — the fields the LLM branches on.
- `test_playbook_generation.py` — asserts the generated PLAYBOOK block
  still contains the contracted actions and their known failure codes,
  so someone can't remove a contract without breaking the test.

## Adding a regression

When you hit a bug in production that traces back to a wrong tool
choice or an untyped error, add a case here before you ship the fix:

1. Reproduce the cursor state (what positions existed, what payload the
   AI sent).
2. Assert the `ValvoActionError` shape (or the playbook content) your
   fix produces.
3. Confirm it fails against the old code, then merge with the fix.

Offline-only by design. Once you want to catch prompt drift / LLM
regressions end-to-end, add a companion `test_live_*.py` that spins up
the real engine with a small model and asserts tool-call sequences.
That adds cost + latency, so keep it separate from this folder.
