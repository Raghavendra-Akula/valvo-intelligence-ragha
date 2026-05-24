-- Classification V2 — manual overrides & data fixes
-- ============================================================
-- Idempotent. Safe to re-run after any reseed of custom_sectors_v2 /
-- themes_v2 or after any reclassify pass. Re-running after the first
-- application is a no-op (the rows match the desired state already).
--
-- WHEN TO RUN
--   • After applying the V2 schema + seeds (`seeds.seed_taxonomy()`)
--     and BEFORE the first full classifier sweep — so the manual rows
--     are in place when the classifier writes its decisions.
--   • Whenever someone reseeds the taxonomy from the Python seed files
--     (which intentionally do NOT carry these overrides — see notes
--     below for why).
--
-- WHY THESE EXIST
-- ----------------------------------------------------------
-- 1. TEJASNET → optical-networking
--    TEJASNET reports a single 100%-revenue segment named "Telecom and
--    data networking related products and services". The V2 keyword
--    matcher routes this generically to `Telecom Services` (a telco
--    bucket). But TEJASNET is a vendor TO telcos — they sell optical
--    transport / DWDM gear — they are NOT a telco. The seed file
--    (config/custom_sectors_seed_v2.py) intentionally leaves
--    optical-networking.name_overrides empty so the classifier
--    *proves* the assignment via segments+concall rather than assuming.
--    For TEJASNET, neither path produces the right answer, so a
--    manual_override edge is the cleanest fix.
--
-- 2. Stripped bad theme tags from `concall_understanding_v2`
--    Gemini's concall pass over-tagged a few stocks because the theme
--    slugs are semantically close to phrases the issuers actually use:
--      • HFCL / STLTECH — `transmission_wires_cables` (= power-cable
--        T&D theme). These companies make OPTICAL FIBER cable, not
--        Polycab-style copper power cable. The theme description scopes
--        to T&D capex; OFC doesn't belong here.
--      • NETWEB — `semiconductors_osat`. Netweb is an HPC/server
--        ASSEMBLY house — they're customers of OSATs / fab houses, not
--        operators in the chip-fab value chain.
--    These rows are scrubbed from both `exposure_json` and
--    `themes_extracted` so a future re-classify will not reintroduce
--    them. Concall ingestion itself is gated off (env var
--    V2_CONCALL_ENABLED=false), so no new bad tags will appear unless
--    that flag is flipped.
-- ============================================================

BEGIN;

-- ─────────────────────────────────────────────────────────────────
-- 1. TEJASNET → optical-networking (sub-sector pin via manual_override)
-- ─────────────────────────────────────────────────────────────────
INSERT INTO stock_custom_sector_v2
    (security_id, custom_sector_id, source, matched_keyword,
     confidence, is_primary, created_at, updated_at)
SELECT su.security_id, cs.id, 'manual_override',
       'curated: telecom-equipment OEM, not a telco',
       1.0, true, NOW(), NOW()
  FROM stock_universe su
  JOIN custom_sectors_v2 cs ON cs.slug = 'optical-networking'
 WHERE upper(su.symbol) = 'TEJASNET'
ON CONFLICT (security_id, custom_sector_id) DO UPDATE
    SET source          = 'manual_override',
        matched_keyword = EXCLUDED.matched_keyword,
        confidence      = EXCLUDED.confidence,
        updated_at      = NOW();

-- Audit-trail evidence row (dedupe-safe via unique source_ref).
INSERT INTO classification_evidence_v2
    (security_id, layer, value_slug, value_text,
     evidence_kind, weight, confidence, matched_term,
     evidence_data, source_ref, author)
SELECT su.security_id, 'sub_sector', 'optical-networking', 'Optical Networking',
       'manual_override', 1.0, 1.0, 'curated TEJASNET pin',
       jsonb_build_object('reason',
         'telecom-equipment OEM, not a telco; segment_keyword telecom matched generic Telecom Services'),
       'manual-override:tejasnet:optical-networking',
       'classification-audit'
  FROM stock_universe su
 WHERE upper(su.symbol) = 'TEJASNET'
   AND NOT EXISTS (
     SELECT 1 FROM classification_evidence_v2 e
      WHERE e.source_ref = 'manual-override:tejasnet:optical-networking'
        AND e.security_id = su.security_id
   );

-- ─────────────────────────────────────────────────────────────────
-- 2. Scrub bad theme tags from Gemini concall understanding
-- ─────────────────────────────────────────────────────────────────
-- HFCL / STLTECH — remove transmission_wires_cables (they make OFC, not power cable)
UPDATE concall_understanding_v2
   SET exposure_json    = exposure_json    - 'transmission_wires_cables',
       themes_extracted = themes_extracted - 'transmission_wires_cables'
 WHERE upper(symbol) IN ('HFCL','STLTECH')
   AND (exposure_json    ?  'transmission_wires_cables'
     OR themes_extracted @> '["transmission_wires_cables"]'::jsonb);

-- NETWEB — remove semiconductors_osat (HPC assembly, not a fab/OSAT operator)
UPDATE concall_understanding_v2
   SET exposure_json    = exposure_json    - 'semiconductors_osat',
       themes_extracted = themes_extracted - 'semiconductors_osat'
 WHERE upper(symbol) = 'NETWEB'
   AND (exposure_json    ?  'semiconductors_osat'
     OR themes_extracted @> '["semiconductors_osat"]'::jsonb);

COMMIT;

-- After this script runs, you should re-classify the affected stocks so
-- their stock_themes_v2 / stock_custom_sector_v2 rows reflect the
-- cleaned source data:
--
--   POST /api/v2/classification/classify
--       { "symbols": ["TEJASNET","HFCL","STLTECH","NETWEB"] }
--
-- or locally:
--
--   from services.classification_v2.classifier import classify_all
--   classify_all(symbols=['TEJASNET','HFCL','STLTECH','NETWEB'])
