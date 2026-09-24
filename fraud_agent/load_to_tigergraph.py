"""
Load the fraud dataset into TigerGraph via MCP tools.

Steps:
  1. Creates schema (vertices + edges)
  2. Loads customers, cards, transactions, identity, closed cases
  3. Creates device profiles and edges
  4. Adds vector embeddings for closed case narratives (GraphRAG)

Run AFTER setting TG_HOST, TG_USERNAME, TG_PASSWORD in fraud_agent/.env:
    python3 load_to_tigergraph.py

Loads a ~10k-row sample by default. Pass --full to load all 590k rows.
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent.mcp_tools import (
    mcp_upsert_vertex, mcp_upsert_edge, mcp_run_gsql,
    is_available, setup_schema_on_tg, TG_GRAPHNAME
)

DATA_DIR = Path(__file__).parent.parent
TXN_CSV = DATA_DIR / "transactions.csv"
ID_CSV = DATA_DIR / "identity.csv"
CASES_CSV = DATA_DIR / "closed_cases_history.csv"
PACK_CSV = DATA_DIR / "case_pack.csv"

BATCH = 200  # upsert batch size


def setup_schema():
    print("Setting up TigerGraph schema...")
    ok = setup_schema_on_tg()
    if not ok:
        print("Schema setup failed. Check TG_HOST credentials and try again.")
        sys.exit(1)


def load_closed_cases():
    print("\nLoading closed cases (GraphRAG memory)...")
    count = 0
    with open(CASES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ok = mcp_upsert_vertex("ClosedCase", row["case_id"], {
                "customer_id": row["customer_id"],
                "card_id": row["card_id"],
                "opened_at": row["opened_at"],
                "closed_at": row["closed_at"],
                "outcome": row["outcome"],
                "pattern": row["pattern"],
                "exposure_usd": float(row["exposure_usd"] or 0),
                "analyst_notes": (row["analyst_notes"] or "")[:500],
            })
            if ok:
                count += 1
                if count % 500 == 0:
                    print(f"  {count} cases...")
    print(f"  ✓ {count} closed cases loaded")


def load_transactions(sample_size: int = 10000):
    print(f"\nLoading transactions (sample={sample_size:,})...")
    seen_customers = set()
    seen_cards = set()
    seen_devices = set()
    txn_count = 0

    # Load identity for device lookups
    print("  Indexing identity records...")
    identity = {}
    with open(ID_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("DeviceInfo"):
                identity[row["TransactionID"]] = row

    with open(TXN_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = row["TransactionID"]
            cid = row["customer_id"]
            card1 = row["card1"]
            card_id = f"{cid}-K1"  # simplified card vertex ID

            # Customer vertex
            if cid not in seen_customers:
                mcp_upsert_vertex("Customer", cid, {})
                seen_customers.add(cid)

            # Card vertex
            if card_id not in seen_cards:
                mcp_upsert_vertex("Card", card_id, {
                    "card1": card1,
                    "card4": row.get("card4", ""),
                    "card6": row.get("card6", ""),
                })
                mcp_upsert_edge("Customer", cid, "OWNS", "Card", card_id)
                seen_cards.add(card_id)

            # Transaction vertex
            mcp_upsert_vertex("Transaction", tid, {
                "ts": row.get("ts", ""),
                "amount": float(row.get("TransactionAmt") or 0),
                "product_cd": row.get("ProductCD", ""),
                "channel": row.get("channel", ""),
                "addr1": float(row.get("addr1") or 0),
                "addr2": float(row.get("addr2") or 0),
                "risk_score": float(row.get("risk_score") or 0),
                "p_emaildomain": row.get("P_emaildomain", ""),
            })
            mcp_upsert_edge("Card", card_id, "MADE", "Transaction", tid)

            # Billing region
            if row.get("addr1"):
                region_id = f"R{row['addr1']}"
                mcp_upsert_vertex("BillingRegion", region_id, {
                    "region_code": row["addr1"]
                })
                mcp_upsert_edge("Transaction", tid, "BILLED_IN", "BillingRegion", region_id)

            # Device profile (online transactions only)
            id_rec = identity.get(tid)
            if id_rec and id_rec.get("DeviceInfo"):
                dev_id = f"D_{id_rec['DeviceInfo'][:60].replace(' ', '_')}"
                if dev_id not in seen_devices:
                    mcp_upsert_vertex("DeviceProfile", dev_id, {
                        "device_info": id_rec.get("DeviceInfo", ""),
                        "device_type": id_rec.get("DeviceType", ""),
                        "id_15": id_rec.get("id_15", ""),
                        "id_23": id_rec.get("id_23", ""),
                        "id_30": id_rec.get("id_30", ""),
                        "id_31": id_rec.get("id_31", ""),
                    })
                    seen_devices.add(dev_id)
                mcp_upsert_edge("Transaction", tid, "FROM_DEVICE", "DeviceProfile", dev_id)

            txn_count += 1
            if txn_count % 500 == 0:
                print(f"  {txn_count:,} / {sample_size:,} transactions...")
            if txn_count >= sample_size:
                break

    print(f"  ✓ {txn_count:,} transactions, {len(seen_customers):,} customers, "
          f"{len(seen_cards):,} cards, {len(seen_devices):,} devices")


def load_case_pack():
    print("\nLoading 20 exam case pack...")
    count = 0
    with open(PACK_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mcp_upsert_vertex("InvestigationCase", row["case_id"], {
                "customer_id": row["customer_id"],
                "card_id": row["card_id"],
                "status": "open",
                "verdict": "pending",
                "fraud_probability": 0.0,
                "pattern": "none",
                "exposure_usd": 0.0,
                "summary": row["trigger_text"][:200],
            })
            count += 1
    print(f"  ✓ {count} exam cases loaded")


def get_stats():
    print("\nGraph statistics:")
    for vtype in ["Customer", "Card", "Transaction", "DeviceProfile",
                  "ClosedCase", "InvestigationCase", "BillingRegion"]:
        gsql = f"""
USE GRAPH {TG_GRAPHNAME}
INTERPRET QUERY () {{
  SumAccum<INT> @@cnt;
  all = SELECT v FROM {vtype}:v ACCUM @@cnt += 1;
  PRINT @@cnt;
}}"""
        result = mcp_run_gsql(gsql)
        try:
            cnt = result.get("results", [{}])[0].get("@@cnt", "?")
        except Exception:
            cnt = "?"
        print(f"  {vtype}: {cnt}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                        help="Load all 590k transactions (slow, ~30min)")
    parser.add_argument("--sample", type=int, default=10000,
                        help="Sample size for transactions (default: 10000)")
    parser.add_argument("--schema-only", action="store_true",
                        help="Only set up schema, don't load data")
    args = parser.parse_args()

    if not is_available():
        print("ERROR: TG_HOST not set in fraud_agent/.env")
        print("  Add: TG_HOST=https://your-workspace.i.tgcloud.io")
        sys.exit(1)

    t0 = time.time()
    print("=" * 50)
    print("TigerGraph Fraud Dataset Loader")
    print(f"Target: {os.environ.get('TG_HOST')} / {os.environ.get('TG_GRAPHNAME', 'FraudGraph')}")
    print("=" * 50)

    setup_schema()

    if not args.schema_only:
        load_closed_cases()
        sample = None if args.full else args.sample
        load_transactions(sample_size=sample or 590742)
        load_case_pack()
        get_stats()

    print(f"\n✓ Done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
