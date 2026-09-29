import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import app
from app.scenarios import ScenarioRequest, simulate
from app.engine.scoring import score_contract
from app.models import Contract, OptionType
from datetime import date, timedelta

def make_contract(bid=3.0, ask=3.2, oi=1000):
    return Contract(underlying="TEST", occ_symbol="TEST", option_type=OptionType.CALL,
                    strike=100, expiration=date.today()+timedelta(days=30), dte=30,
                    bid=bid, ask=ask, last=(bid+ask)/2, volume=500, open_interest=oi)


def test_expiry_call_put_payoff_and_fees_scale():
    for side,spot,intrinsic in [("call",110,10),("put",90,10)]:
        doc=simulate(ScenarioRequest(option_type=side,spot=spot,strike=100,premium=3,dte=0,elapsed_days=0,contracts=2,round_trip_fee_per_contract=1,move_min_pct=-1,move_max_pct=1,points=3))
        row=doc["rows"][1]
        assert row["expiry_intrinsic_per_share"] == intrinsic
        assert row["theoretical_per_share"] == intrinsic
        assert row["theoretical_pnl_usd"] == 1398
        assert doc["summary"]["max_loss_usd"] == 602


def test_monotonic_spot_vol_and_put_call_parity():
    from app.engine.blackscholes import bs_price
    from app.models import OptionType
    import math
    req=ScenarioRequest(points=5)
    doc=simulate(req)
    assert sorted(r["theoretical_per_share"] for r in doc["rows"]) == [r["theoretical_per_share"] for r in doc["rows"]]
    richer=simulate(req.model_copy(update={"iv_change":.1}))
    assert richer["rows"][2]["theoretical_per_share"]>doc["rows"][2]["theoretical_per_share"]
    T=(req.dte-req.elapsed_days)/365
    call=doc["rows"][2]["theoretical_per_share"]
    put=bs_price(req.spot,req.strike,req.risk_free_rate,req.dividend_yield,req.iv,T,OptionType.PUT)
    assert call-put == pytest.approx(req.spot*math.exp(-req.dividend_yield*T)-req.strike*math.exp(-req.risk_free_rate*T))


def test_validation_zero_iv_and_unreachable_put_break_even():
    for change in [{"spot":0},{"iv":float("nan")},{"elapsed_days":31},{"iv_change":-.5},{"move_min_pct":-100},{"points":10000},{"contracts":True},{"extra":1}]:
        with pytest.raises(ValidationError): ScenarioRequest(**change)
    zero=simulate(ScenarioRequest(iv=0))
    assert all(r["theoretical_per_share"]>=0 for r in zero["rows"])
    put=simulate(ScenarioRequest(option_type="put",premium=101))
    assert put["summary"]["expiry_break_even"] is None


def test_grade_contributions_exactly_reconcile_and_fallbacks_are_disclosed():
    for contract in [make_contract(),make_contract(bid=.1,ask=1.2,oi=5)]:
        sc=score_contract(contract,100,.04,0,hv=None)
        trace=sc.to_dict()["score_trace"]
        assert sum(c["contribution"] for c in trace["components"]) == pytest.approx(trace["weighted_score"])
        assert trace["final_score"] == sc.overall_score
        assert trace["weighted_score"]-trace["liquidity_penalty"] == pytest.approx(sc.overall_score)
        assert any(c["basis"]=="fallback: missing input" for c in trace["components"])
        assert sum(c["normalized_weight"] for c in trace["components"]) == pytest.approx(1)


def test_api_reports_are_deterministic_network_free_and_escape_input(monkeypatch):
    import app.api as api
    monkeypatch.setattr(api,"get_provider",lambda:(_ for _ in ()).throw(AssertionError("provider called")))
    req={"label":"<script>alert(1)</script>"}
    client=TestClient(app)
    first=client.post("/scenario",json=req)
    assert first.status_code==200
    assert first.json()==client.post("/scenario",json=req).json()
    assert first.json()["provenance"]["inputs_sha256"] != client.post("/scenario",json={"premium":4}).json()["provenance"]["inputs_sha256"]
    html=client.post("/scenario/report?format=html",json=req)
    assert "<script>alert(1)</script>" not in html.text
    assert "&lt;script&gt;" in html.text
    assert "sandbox" in html.headers["content-security-policy"]
    csv_response=client.post("/scenario/report?format=csv",json=req)
    rows=list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(rows)==61
    exported=client.post("/scenario/report?format=json",json=req)
    assert json.loads(exported.text)==first.json()
    assert client.post("/scenario/report?format=unsupported",json=req).status_code==422
