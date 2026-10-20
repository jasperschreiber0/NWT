-- Additive only. Apply to an isolated copy first; no historical backfill.
BEGIN;
CREATE TABLE IF NOT EXISTS nwt_entry_evidence (
 position_id UUID PRIMARY KEY REFERENCES nwt_portfolio_ledger(position_id),
 decision_id UUID REFERENCES nwt_decision_inputs(id),
 eligible BOOLEAN NOT NULL,
 snapshot JSONB NOT NULL,
 recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK (NOT eligible OR decision_id IS NOT NULL)
);
CREATE OR REPLACE FUNCTION nwt_entry_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Entry evidence is immutable; retain raw evidence and append a reviewed correction separately'; END;
$$;
DROP TRIGGER IF EXISTS nwt_entry_evidence_immutable ON nwt_entry_evidence;
CREATE TRIGGER nwt_entry_evidence_immutable BEFORE UPDATE OR DELETE ON nwt_entry_evidence
 FOR EACH ROW EXECUTE FUNCTION nwt_entry_evidence_immutable();
ALTER TABLE nwt_trade_outcomes ADD COLUMN IF NOT EXISTS evidence_version TEXT;
-- A legacy outcome is never made eligible merely by attaching a new snapshot.
CREATE OR REPLACE VIEW nwt_verified_learning_outcomes AS
 SELECT o.* FROM nwt_trade_outcomes o
 JOIN nwt_portfolio_ledger l ON l.position_id=o.position_id
 JOIN nwt_entry_evidence e ON e.position_id=l.position_id
 WHERE e.eligible AND o.evidence_version='entry-evidence-20261020-v1'
 AND l.status='closed'
 AND jsonb_array_length(e.snapshot->'expected_assets') > 0
 AND NOT EXISTS (
   SELECT 1 FROM jsonb_array_elements_text(e.snapshot->'expected_assets') expected(asset)
   WHERE NOT EXISTS (SELECT 1 FROM nwt_portfolio_ledger leg
     WHERE COALESCE(leg.spread_group_id,leg.position_id)=COALESCE(l.spread_group_id,l.position_id)
       AND leg.asset=expected.asset)
 )
 AND (SELECT count(*) FROM nwt_trade_outcomes other WHERE other.position_id=o.position_id)=1
 AND NOT EXISTS (
   SELECT 1 FROM nwt_portfolio_ledger leg
   WHERE COALESCE(leg.spread_group_id,leg.position_id)=COALESCE(l.spread_group_id,l.position_id)
   AND (leg.status<>'closed' OR NOT EXISTS (
     SELECT 1 FROM nwt_trade_outcomes lo JOIN nwt_entry_evidence le ON le.position_id=lo.position_id
     WHERE lo.position_id=leg.position_id AND le.eligible
       AND le.decision_id=e.decision_id AND lo.evidence_version='entry-evidence-20261020-v1'
       AND (SELECT count(*) FROM nwt_trade_outcomes dup WHERE dup.position_id=leg.position_id)=1
   ))
 );
COMMENT ON VIEW nwt_verified_learning_outcomes IS
 'Only newly verified, complete trade groups. Historical raw outcomes preserved, excluded by default.';
COMMIT;
