"""
TigerGraph client — connects to Savanna/CE and runs GSQL queries.
Also manages schema creation and data loading via the REST API.

Set these in fraud_agent/.env:
  TG_HOST=https://your-workspace.i.tgcloud.io
  TG_USERNAME=tigergraph
  TG_PASSWORD=your-password
  TG_GRAPHNAME=FraudGraph

If TG_HOST is not set, falls back to local SQLite (development mode).
"""

import os
from typing import Optional
from pathlib import Path

# Load .env
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TG_HOST = os.environ.get("TG_HOST", "")
TG_USERNAME = os.environ.get("TG_USERNAME", "tigergraph")
TG_PASSWORD = os.environ.get("TG_PASSWORD", "")
TG_GRAPHNAME = os.environ.get("TG_GRAPHNAME", "FraudGraph")

_conn = None


def get_connection():
    """Get or create TigerGraph connection."""
    global _conn
    if _conn is not None:
        return _conn
    if not TG_HOST:
        return None
    try:
        import pyTigerGraph as tg
        _conn = tg.TigerGraphConnection(
            host=TG_HOST,
            username=TG_USERNAME,
            password=TG_PASSWORD,
            graphname=TG_GRAPHNAME,
        )
        _conn.getToken(_conn.createSecret())
        print(f"[TigerGraph] Connected to {TG_HOST} graph={TG_GRAPHNAME}")
        return _conn
    except Exception as e:
        print(f"[TigerGraph] Connection failed: {e} — falling back to SQLite")
        return None


def is_connected() -> bool:
    return get_connection() is not None


# ─────────────────────────────────────────────────────────────────────────────
# Schema setup — run once after connecting
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA_GSQL = """
USE GLOBAL
DROP GRAPH FraudGraph

CREATE VERTEX Customer (PRIMARY_ID customer_id STRING) WITH STATS="OUTDEGREE_BY_EDGETYPE", PRIMARY_ID_AS_ATTRIBUTE="true"
CREATE VERTEX Card (PRIMARY_ID card_id STRING, card1 STRING, card4 STRING, card6 STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
CREATE VERTEX Transaction (
  PRIMARY_ID txn_id STRING, ts STRING, amount FLOAT,
  product_cd STRING, channel STRING, addr1 FLOAT, addr2 FLOAT,
  risk_score FLOAT, p_emaildomain STRING
) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
CREATE VERTEX DeviceProfile (
  PRIMARY_ID device_id STRING, device_info STRING,
  device_type STRING, id_15 STRING, id_23 STRING, id_30 STRING, id_31 STRING
) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
CREATE VERTEX BillingRegion (PRIMARY_ID region_code STRING) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
CREATE VERTEX ClosedCase (
  PRIMARY_ID case_id STRING, customer_id STRING, card_id STRING,
  opened_at STRING, closed_at STRING, outcome STRING, pattern STRING,
  exposure_usd FLOAT, analyst_notes STRING
) WITH PRIMARY_ID_AS_ATTRIBUTE="true"
CREATE VERTEX InvestigationCase (
  PRIMARY_ID case_id STRING, customer_id STRING, card_id STRING,
  status STRING, verdict STRING, fraud_probability FLOAT,
  pattern STRING, exposure_usd FLOAT, summary STRING
) WITH PRIMARY_ID_AS_ATTRIBUTE="true"

CREATE DIRECTED EDGE OWNS (FROM Customer, TO Card)
CREATE DIRECTED EDGE MADE (FROM Card, TO Transaction)
CREATE DIRECTED EDGE FROM_DEVICE (FROM Transaction, TO DeviceProfile)
CREATE DIRECTED EDGE BILLED_IN (FROM Transaction, TO BillingRegion)
CREATE DIRECTED EDGE INVOLVES (FROM ClosedCase, TO Transaction)
CREATE DIRECTED EDGE ON_CARD (FROM ClosedCase, TO Card)
CREATE DIRECTED EDGE CASE_INVOLVES (FROM InvestigationCase, TO Transaction)
CREATE DIRECTED EDGE CASE_ON_CARD (FROM InvestigationCase, TO Card)
CREATE UNDIRECTED EDGE SHARES_DEVICE (FROM Card, TO Card, device_id STRING)

CREATE GRAPH FraudGraph (Customer, Card, Transaction, DeviceProfile,
  BillingRegion, ClosedCase, InvestigationCase,
  OWNS, MADE, FROM_DEVICE, BILLED_IN, INVOLVES, ON_CARD,
  CASE_INVOLVES, CASE_ON_CARD, SHARES_DEVICE)
"""


def setup_schema():
    conn = get_connection()
    if not conn:
        print("[TigerGraph] Not connected — skipping schema setup")
        return False
    try:
        conn.gsql(SCHEMA_GSQL)
        print("[TigerGraph] Schema created")
        return True
    except Exception as e:
        print(f"[TigerGraph] Schema error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Graph write — write a completed investigation case
# ─────────────────────────────────────────────────────────────────────────────

def write_case_to_tigergraph(case: dict) -> bool:
    """
    Upsert an InvestigationCase vertex and its edges into TigerGraph.
    Called by the agent after each investigation.
    """
    conn = get_connection()
    if not conn:
        return False
    try:
        case_id = case.get("case_id", "")
        conn.upsertVertex("InvestigationCase", case_id, {
            "customer_id": case.get("customer_id", ""),
            "card_id": case.get("card_id", ""),
            "status": case.get("status", ""),
            "verdict": case.get("verdict", ""),
            "fraud_probability": float(case.get("fraud_probability", 0)),
            "pattern": case.get("pattern", "none"),
            "exposure_usd": float(case.get("exposure_usd", 0)),
            "summary": case.get("summary", ""),
        })
        # Link case → affected transactions
        for txn_id in case.get("affected_txn_ids", []):
            conn.upsertEdge("InvestigationCase", case_id, "CASE_INVOLVES", "Transaction", str(txn_id))
        # Link case → card
        card_id = case.get("card_id", "")
        if card_id:
            conn.upsertEdge("InvestigationCase", case_id, "CASE_ON_CARD", "Card", card_id)
        print(f"[TigerGraph] Case {case_id} written to graph")
        return True
    except Exception as e:
        print(f"[TigerGraph] Write error for {case.get('case_id')}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# GSQL query wrappers — mirror the SQLite graph_queries API
# ─────────────────────────────────────────────────────────────────────────────

def tg_get_similar_cases(customer_id: str, pattern: str = None, limit: int = 5) -> list[dict]:
    """Retrieve similar closed cases from TigerGraph."""
    conn = get_connection()
    if not conn:
        return []
    try:
        gsql = f"""
        USE GRAPH {TG_GRAPHNAME}
        INTERPRET QUERY () {{
          cases = SELECT c FROM ClosedCase:c
                  WHERE c.customer_id == "{customer_id}"
                  {"OR c.pattern == " + f'"{pattern}"' if pattern else ""}
                  ORDER BY c.closed_at DESC
                  LIMIT {limit};
          PRINT cases;
        }}"""
        result = conn.gsql(gsql)
        return result.get("results", [{}])[0].get("cases", [])
    except Exception as e:
        print(f"[TigerGraph] Query error: {e}")
        return []


def tg_get_device_neighbors(device_info: str, limit: int = 20) -> list[dict]:
    """Find all cards that used this device profile."""
    conn = get_connection()
    if not conn:
        return []
    try:
        # Use REST++ upsert-based query or interpret query
        gsql = f"""
        USE GRAPH {TG_GRAPHNAME}
        INTERPRET QUERY () {{
          devices = SELECT d FROM DeviceProfile:d
                    WHERE d.device_info LIKE "%{device_info[:30]}%"
                    LIMIT 5;
          cards = SELECT c FROM devices:d -(FROM_DEVICE<-)- :t -(MADE<-)- :c
                  LIMIT {limit};
          PRINT cards;
        }}"""
        result = conn.gsql(gsql)
        return result.get("results", [{}])[0].get("cards", [])
    except Exception as e:
        print(f"[TigerGraph] Device query error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Data loading — load CSV data into TigerGraph in batches
# ─────────────────────────────────────────────────────────────────────────────

def load_closed_cases_to_tg(cases_csv: str):
    """Bulk load closed cases into TigerGraph ClosedCase vertices."""
    conn = get_connection()
    if not conn:
        print("[TigerGraph] Not connected")
        return
    import csv
    print("Loading closed cases into TigerGraph...")
    batch, BATCH = [], 500
    with open(cases_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            batch.append({
                "case_id": row["case_id"],
                "customer_id": row["customer_id"],
                "card_id": row["card_id"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "outcome": row["outcome"],
                "pattern": row["pattern"],
                "exposure_usd": float(row["exposure_usd"] or 0),
                "analyst_notes": row["analyst_notes"][:500] if row["analyst_notes"] else "",
            })
            if len(batch) >= BATCH:
                for c in batch:
                    try:
                        conn.upsertVertex("ClosedCase", c["case_id"], {k: v for k, v in c.items() if k != "case_id"})
                    except Exception:
                        pass
                print(f"  Loaded {len(batch)} cases...")
                batch = []
    for c in batch:
        try:
            conn.upsertVertex("ClosedCase", c["case_id"], {k: v for k, v in c.items() if k != "case_id"})
        except Exception:
            pass
    print("[TigerGraph] Closed cases loaded")
