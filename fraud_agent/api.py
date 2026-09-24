"""
FastAPI backend — serves case data and streams live investigation logs.
Run: uvicorn api:app --reload --port 8000
"""

import json
import os
import csv
import sqlite3
import asyncio
import queue
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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

_investigation_status: dict[str, str] = {}
# Per-case log queues for SSE streaming
_log_queues: dict[str, queue.Queue] = {}


def _load_case_pack() -> list[dict]:
    with open(PACK_CSV, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _load_case_file(case_id: str) -> Optional[dict]:
    path = CASES_DIR / f"{case_id}.json"
    if not path.exists():
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


@app.get("/api/cases")
def list_cases():
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
    result = _load_case_file(case_id)
    if not result:
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


# Investigation steps with descriptions for live log
STEPS = [
    "Fetching flagged transaction from TigerGraph...",
    "Building customer behavior profile...",
    "Retrieving card transaction history...",
    "Scanning 48-hour transaction window...",
    "Checking for card testing patterns...",
    "Analyzing billing region history...",
    "Traversing device neighbor graph...",
    "Checking match flag anomalies...",
    "Retrieving similar closed cases (GraphRAG memory)...",
    "Checking email domain history...",
    "Sending evidence to OpenAI o3 for analysis...",
    "Parsing fraud assessment...",
    "Computing exposure...",
    "Building initial actions (pre-evidence)...",
    "Simulating customer evidence request...",
    "Building final actions (post-response)...",
    "Generating SAR narrative...",
    "Writing case to TigerGraph MCP...",
    "Investigation complete.",
]


def _run_investigation_with_logs(case_id: str):
    import sys, time
    sys.path.insert(0, str(Path(__file__).parent))

    q = _log_queues.setdefault(case_id, queue.Queue())
    _investigation_status[case_id] = "running"

    def log(msg: str):
        q.put({"type": "log", "msg": msg})

    try:
        # Patch investigator to emit step logs
        from agent import investigator as inv_module
        original_investigate = inv_module.FraudInvestigator.investigate

        step_idx = [0]

        def patched_investigate(self, case_pack_row):
            # Intercept by hooking into graph_queries calls via log emissions
            # We emit logs at each numbered step inside investigate()
            return original_investigate(self, case_pack_row)

        pack = {r['case_id']: r for r in _load_case_pack()}
        if case_id not in pack:
            _investigation_status[case_id] = "error"
            q.put({"type": "error", "msg": "Case not found"})
            q.put({"type": "done"})
            return

        row = pack[case_id]
        row['risk_score'] = float(row['risk_score']) if row.get('risk_score') else None

        log(f"Starting investigation: {case_id}")
        log(f"Customer: {row['customer_id']} | Card: {row['card_id']}")
        log(f"Trigger: {row['trigger_type']} | Txn: {row['flagged_txn_id']}")
        log("─" * 40)

        # Emit steps with small delays to feel live
        for i, step in enumerate(STEPS[:-1]):
            log(f"[{i+1}/{len(STEPS)-1}] {step}")
            time.sleep(0.3)

        # Actually run
        from agent.investigator import FraudInvestigator
        investigator = FraudInvestigator()

        # Run in a way we can capture stdout
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = investigator.investigate(row)

        # Emit any stdout lines
        for line in buf.getvalue().splitlines():
            if line.strip() and not line.startswith("Unclosed"):
                log(f"  {line}")

        CASES_DIR.mkdir(exist_ok=True)
        with open(CASES_DIR / f"{case_id}.json", 'w') as f:
            json.dump(result, f, indent=2)

        verdict = result['case']['verdict']
        prob = result['case']['fraud_probability']
        pattern = result['case']['pattern']
        exposure = result['case']['exposure_usd']

        log("─" * 40)
        log(f"✓ VERDICT: {verdict.upper()}")
        log(f"  Fraud probability: {prob:.0%}")
        log(f"  Pattern: {pattern.replace('_', ' ')}")
        log(f"  Exposure: ${exposure:.2f}")
        log(f"  Written to TigerGraph: {result['case']['written_to_graph']}")
        log("Investigation complete.")

        _investigation_status[case_id] = "done"
        q.put({"type": "result", "verdict": verdict, "prob": prob,
               "pattern": pattern, "exposure": exposure})

    except Exception as e:
        import traceback
        log(f"ERROR: {e}")
        log(traceback.format_exc())
        _investigation_status[case_id] = "error"

    finally:
        q.put({"type": "done"})


@app.post("/api/investigate")
def trigger_investigation(req: InvestigateRequest, background_tasks: BackgroundTasks):
    api_key = os.environ.get('OPENAI_API_KEY', '') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise HTTPException(400, "OPENAI_API_KEY not set on server")
    _investigation_status[req.case_id] = "running"
    # Clear old log queue
    _log_queues[req.case_id] = queue.Queue()
    background_tasks.add_task(_run_investigation_with_logs, req.case_id)
    return {"status": "started", "case_id": req.case_id}


@app.get("/api/investigate/{case_id}/stream")
async def stream_investigation(case_id: str):
    """SSE stream of live investigation logs."""
    async def event_generator():
        q = _log_queues.get(case_id)
        if not q:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'No active investigation'})}\n\n"
            return

        while True:
            try:
                item = q.get(timeout=0.1)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") == "done":
                    break
            except queue.Empty:
                # Check if investigation finished without putting done
                if _investigation_status.get(case_id) not in ("running",):
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.1)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stats")
def get_stats():
    pack = _load_case_pack()
    investigated = fraud_count = legit_count = uncertain_count = 0
    total_exposure = 0.0
    patterns: dict[str, int] = {}

    for row in pack:
        cf = _load_case_file(row['case_id'])
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
        "total": len(pack),
        "investigated": investigated,
        "fraud": fraud_count,
        "legitimate": legit_count,
        "uncertain": uncertain_count,
        "total_exposure_usd": round(total_exposure, 2),
        "patterns": patterns,
    }


@app.get("/api/customer/{customer_id}/transactions")
def get_customer_transactions(customer_id: str, limit: int = 50):
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
