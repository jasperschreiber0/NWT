import json
from datetime import datetime, timezone, timedelta
import pytest
import earnings_variance as ev


class Clock(datetime):
    current=datetime(2026,10,20,tzinfo=timezone.utc)
    @classmethod
    def now(cls,tz=None):return cls.current


def features():
    return dict(event_id='AAPL-20261103',symbol='AAPL',cutoff='2026-11-03T21:00:00Z',
                source_text='Management reports unresolved demand uncertainty.',quote='unresolved demand uncertainty',
                source_reference='issuer-cached-sha',source_published_at='2026-11-03T12:00:00Z',
                features_available_at='2026-11-03T15:00:00Z',model_output_at='2026-11-03T15:30:00Z',
                model='existing-output-model-id',prior_variance=.0001,earnings_gap=.02,uncertainty_score=.8,
                inference_cost_usd=None,prior_returns=[.01]*20,
                prior_session_dates=[(datetime(2026,10,1,tzinfo=timezone.utc)+timedelta(days=i)).isoformat() for i in range(20)],
                earnings_open_at='2026-11-03T14:30:00Z',
                session_dates=['2026-11-04T21:00:00Z','2026-11-05T21:00:00Z','2026-11-06T21:00:00Z','2026-11-09T21:00:00Z','2026-11-10T21:00:00Z'],
                data_source_reference='cached-prices-and-exchange-calendar-sha')


@pytest.fixture
def registered(tmp_path,monkeypatch):
    monkeypatch.setattr(ev,'datetime',Clock)
    Clock.current=datetime(2026,10,20,tzinfo=timezone.utc)
    f=features()
    ev.register(tmp_path,dict(source_reference='SYNTHETIC-SMOKE-NOT-RESEARCH',evidence_class='engineering_smoke',
        events={f['event_id']:{k:f[k] for k in ('symbol','cutoff','earnings_open_at','prior_session_dates','session_dates')}}))
    Clock.current=datetime(2026,11,3,16,tzinfo=timezone.utc)
    return tmp_path


def test_fixed_forecast_and_prospective_single_use(registered):
    f=features();p=ev.seal_prediction(registered,f)
    assert p['prediction']['baseline']==pytest.approx(.00018)
    assert p['prediction']['candidate']==pytest.approx(.000252)
    Clock.current+=timedelta(seconds=1)
    with pytest.raises(FileExistsError):ev.seal_prediction(registered,f)
    with pytest.raises(ValueError,match='not complete'):ev.evaluate(registered,[])
    Clock.current=datetime(2027,4,2,tzinfo=timezone.utc)
    outcome=dict(event_id=f['event_id'],session_dates=f['session_dates'],closes=[100,101,100,101,100,101],
                 source_reference='held-out-prices-sha',corporate_actions_checked=True)
    result=ev.evaluate(registered,[outcome])
    assert result['events']==1 and not result['accepted'] and not result['costs_complete']
    assert result['profitability_validated'] is False
    with pytest.raises(FileExistsError):ev.evaluate(registered,[outcome])


@pytest.mark.parametrize('change', [{'model_output_at':'2026-10-01T00:00:00Z'},
                                    {'cutoff':'2026-11-03T15:00:00Z'}, {'quote':'invented quote'},
                                    {'future_variance':.03}, {'prior_variance':.2}])
def test_leakage_and_grounding_rejected_with_attempt_receipt(registered,change):
    with pytest.raises(ValueError):ev.seal_prediction(registered,dict(features(),**change))
    assert len(list(registered.glob('attempt-*.json')))==1
    assert not list(registered.glob('prediction-*.json'))


def test_missing_outcomes_cannot_be_cherry_picked(registered):
    ev.seal_prediction(registered,features())
    Clock.current=datetime(2027,4,2,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match='Incomplete'):ev.evaluate(registered,[])
    assert (registered/'evaluation-attempt.json').exists()
