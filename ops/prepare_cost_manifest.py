"""Preserve legacy billing notes while exposing explicit, unverified cost coverage."""
import argparse,json
from pathlib import Path
from cost_accounting import REQUIRED_SERVICES
from paper_io import atomic

def prepare(config):
    if 'records' in config or 'coverage' in config:
        return config  # Never replace entered invoice evidence.
    return dict(schema_version=1,records=[],coverage={s:dict(reconciled=False) for s in REQUIRED_SERVICES},
        legacy_notes=config,
        user_reported_combined_cost=dict(services=['alpaca','hetzner'],amount=111,frequency='monthly',
            currency=None,period_start=None,period_end=None,allocation_fraction=None,
            source='User conversation: combined $111; frequency confirmed monthly on 2026-10-27',
            status='Context only; currency, billing dates and allocation unverified; excluded from USD totals'),
        instructions='Add invoice records and reconciled coverage using ops/cost_accounting.py schema. Unknown costs are never zero or verified.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('path',type=Path);args=parser.parse_args()
    prior=json.loads(args.path.read_text()) if args.path.exists() else {}
    atomic(args.path,prepare(prior))
