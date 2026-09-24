"""
FastAPI backend — serves case data to the UI and exposes an endpoint
to trigger investigations on-demand.
Run: uvicorn api:app --reload --port 8000
"""

import json
import os
import csv
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Fraud Investigation Agent API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CASES_DIR = Path(__file__).parent / "cases"
DATA_DIR = Path(__file__).parent.parent
PACK_CSV = DATA_DIR / "case_pack.csv"
DB_PATH = Path(__file__).parent / "data" / "fraud.db"

_investigation_status: dict[str, str] = {}  # case_id → running/done/error


def _load_case_pack() -> list[dict]:
    rows = []
    with open(PACK_CSV, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def _load_case_file(case_id: str) -> Optional[dict]:
    path = CASES_DIR / f"{case_id}.json"
    if not path.exists():
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


@app.get("/api/cases")
def list_cases():
    """List all 20 exam cases with their current status."""
    pack = _load_case_pack()
    result = []
    for row in pack:
        cid = row['case_id']
        case_file = _load_case_file(cid)
        result.append({
            "case_id": cid,
            "opened_at": row['opened_at'],
            "trigger_type": row['trigger_type'],
            "trigger_text": row['trigger_text'],
            "flagged_txn_id": row['flagged_txn_id'],
            "card_id": row['card_id'],
            "customer_id": row['customer_id'],
            "risk_score": row.get('risk_score') or None,
            "investigated": case_file is not None,
            "verdict": case_file['case']['verdict'] if case_file else None,
            "pattern": case_file['case']['pattern'] if case_file else None,
            "fraud_probability": case_file['case']['fraud_probability'] if case_file else None,
            "exposure_usd": case_file['case']['exposure_usd'] if case_file else None,
            "status": case_file['case']['status'] if case_file else "pending",
            "investigation_status": _investigation_status.get(cid, "pending"),
        })
    return result


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    """Get full investigation output for a case."""
    result = _load_case_file(case_id)
    if not result:
        # Return the case pack info
        pack = {r['case_id']: r for r in _load_case_pack()}
        if case_id not in pack:
            raise HTTPException(404, f"Case {case_id} not found")
        row = pack[case_id]
        return {
            "case_id": case_id,
            "trigger_type": row['trigger_type'],
            "trigger_text": row['trigger_text'],
            "investigated": False,
            "investigation_status": _investigation_status.get(case_id, "pending"),
        }
    return result


class InvestigateRequest(BaseModel):
    case_id: str


def _run_investigation(case_id: str):
    """Background task to run a single investigation."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from agent.investigator import FraudInvestigator

    _investigation_status[case_id] = "running"
    try:
        pack = {r['case_id']: r for r in _load_case_pack()}
        if case_id not in pack:
            _investigation_status[case_id] = "error"
            return
        row = pack[case_id]
        row['risk_score'] = float(row['risk_score']) if row.get('risk_score') else None

        investigator = FraudInvestigator()
        result = investigator.investigate(row)

        CASES_DIR.mkdir(exist_ok=True)
        with open(CASES_DIR / f"{case_id}.json", 'w') as f:
            json.dump(result, f, indent=2)
        _investigation_status[case_id] = "done"
    except Exception as e:
        print(f"Investigation error for {case_id}: {e}")
        _investigation_status[case_id] = "error"


@app.post("/api/investigate")
def trigger_investigation(req: InvestigateRequest, background_tasks: BackgroundTasks):
    """Trigger an investigation for a case (runs in background)."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise HTTPException(400, "ANTHROPIC_API_KEY not set on server")
    _investigation_status[req.case_id] = "running"
    background_tasks.add_task(_run_investigation, req.case_id)
    return {"status": "started", "case_id": req.case_id}


@app.get("/api/stats")
def get_stats():
    """Dashboard statistics."""
    pack = _load_case_pack()
    total = len(pack)
    investigated = 0
    fraud_count = 0
    legit_count = 0
    uncertain_count = 0
    total_exposure = 0.0
    patterns: dict[str, int] = {}

    for row in pack:
        cid = row['case_id']
        cf = _load_case_file(cid)
        if cf:
            investigated += 1
            v = cf['case']['verdict']
            if v == 'fraud':
                fraud_count += 1
                total_exposure += cf['case']['exposure_usd']
            elif v == 'legitimate':
                legit_count += 1
            else:
                uncertain_count += 1
            p = cf['case']['pattern']
            patterns[p] = patterns.get(p, 0) + 1

    return {
        "total": total,
        "investigated": investigated,
        "fraud": fraud_count,
        "legitimate": legit_count,
        "uncertain": uncertain_count,
        "total_exposure_usd": round(total_exposure, 2),
        "patterns": patterns,
    }


@app.get("/api/customer/{customer_id}/transactions")
def get_customer_transactions(customer_id: str, limit: int = 50):
    """Fetch recent transactions for a customer (for UI detail view)."""
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(str(DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT TransactionID, ts, TransactionAmt, ProductCD, channel, "
            "addr1, risk_score, card1, card4, card6 "
            "FROM transactions WHERE customer_id=? ORDER BY ts DESC LIMIT ?",
            (customer_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"status": "ok", "db_exists": DB_PATH.exists()}
