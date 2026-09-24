"""
Graph query layer — all data access for the fraud agent.
Mimics what TigerGraph GSQL queries would expose via MCP.
Each function returns clean Python dicts, never raw DB rows.
"""

import sqlite3
import os
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'fraud.db')


def _con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


# ─────────────────────────────────────────────
# Core entity lookups
# ─────────────────────────────────────────────

def get_transaction(txn_id: str) -> Optional[dict]:
    """Fetch a single transaction with its identity record."""
    with _con() as con:
        row = con.execute(
            "SELECT t.*, i.DeviceType, i.DeviceInfo, i.id_15, i.id_23, i.id_30, i.id_31, i.id_33, i.id_34 "
            "FROM transactions t LEFT JOIN identity i ON t.TransactionID=i.TransactionID "
            "WHERE t.TransactionID=?", (str(txn_id),)
        ).fetchone()
        return dict(row) if row else None


def get_card_history(customer_id: str, card1: Optional[str] = None,
                     before_ts: Optional[str] = None, limit: int = 200) -> list[dict]:
    """All transactions for a customer, optionally filtered by card1 and time."""
    with _con() as con:
        params = [customer_id]
        sql = (
            "SELECT t.TransactionID, t.ts, t.TransactionAmt, t.ProductCD, "
            "t.channel, t.addr1, t.addr2, t.card1, t.card4, t.card6, "
            "t.risk_score, t.P_emaildomain, t.M1, t.M2, t.M3, t.M4, t.M5, "
            "t.D1, t.C1, t.C2, t.C6, t.C11, t.C14, "
            "i.DeviceType, i.DeviceInfo, i.id_15, i.id_23, i.id_30, i.id_31, i.id_33 "
            "FROM transactions t LEFT JOIN identity i ON t.TransactionID=i.TransactionID "
            "WHERE t.customer_id=?"
        )
        if card1:
            sql += " AND t.card1=?"
            params.append(card1)
        if before_ts:
            sql += " AND t.ts<=?"
            params.append(before_ts)
        sql += f" ORDER BY t.ts DESC LIMIT {limit}"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_transaction_window(card1: str, center_ts: str, hours: int = 48) -> list[dict]:
    """Transactions on a card (by card1) within ±hours of a timestamp."""
    with _con() as con:
        rows = con.execute(
            "SELECT t.TransactionID, t.ts, t.TransactionAmt, t.ProductCD, "
            "t.channel, t.addr1, t.customer_id, t.card1, t.risk_score, "
            "i.DeviceType, i.DeviceInfo, i.id_15, i.id_23 "
            "FROM transactions t LEFT JOIN identity i ON t.TransactionID=i.TransactionID "
            "WHERE t.card1=? "
            "AND t.ts BETWEEN datetime(?, ?||' hours') AND datetime(?, ?||' hours') "
            "ORDER BY t.ts",
            (card1, center_ts, f"-{hours}", center_ts, f"+{hours}")
        ).fetchall()
        return [dict(r) for r in rows]


def get_device_neighbors(device_info: str, before_ts: Optional[str] = None,
                         window_days: int = 60) -> list[dict]:
    """All cards/customers that used a given DeviceInfo string near a time."""
    if not device_info or device_info.strip() == '':
        return []
    with _con() as con:
        params = [f"%{device_info.strip()}%"]
        sql = (
            "SELECT DISTINCT t.customer_id, t.card1, t.ts, t.TransactionID, "
            "i.DeviceInfo, i.id_15 "
            "FROM identity i JOIN transactions t ON i.TransactionID=t.TransactionID "
            "WHERE i.DeviceInfo LIKE ?"
        )
        if before_ts:
            sql += " AND t.ts<=?"
            params.append(before_ts)
        sql += " ORDER BY t.ts DESC LIMIT 100"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_region_history(addr1: float, customer_id: str,
                       before_ts: Optional[str] = None) -> dict:
    """How many times this customer used this billing region vs. all regions."""
    with _con() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE customer_id=?" +
            (" AND ts<=?" if before_ts else ""),
            ([customer_id, before_ts] if before_ts else [customer_id])
        ).fetchone()[0]
        in_region = con.execute(
            "SELECT COUNT(*), SUM(TransactionAmt) FROM transactions "
            "WHERE customer_id=? AND addr1=?" +
            (" AND ts<=?" if before_ts else ""),
            ([customer_id, addr1, before_ts] if before_ts else [customer_id, addr1])
        ).fetchone()
        # Get all regions this customer has used
        regions = con.execute(
            "SELECT addr1, COUNT(*) as cnt FROM transactions "
            "WHERE customer_id=? AND addr1 IS NOT NULL " +
            ("AND ts<=? " if before_ts else "") +
            "GROUP BY addr1 ORDER BY cnt DESC LIMIT 20",
            ([customer_id, before_ts] if before_ts else [customer_id])
        ).fetchall()
        return {
            "addr1": addr1,
            "total_txns": total,
            "txns_in_this_region": in_region[0],
            "amount_in_region": in_region[1] or 0,
            "familiar_regions": [{"addr1": r[0], "count": r[1]} for r in regions],
            "is_new_region": in_region[0] == 0,
        }


def get_card_testing_signals(card1: str, center_ts: str) -> dict:
    """Look for card-testing pattern: 3+ small online txns in 1hr then a larger one."""
    with _con() as con:
        rows = con.execute(
            "SELECT TransactionID, ts, TransactionAmt, ProductCD, channel "
            "FROM transactions WHERE card1=? "
            "AND ts BETWEEN datetime(?, '-2 hours') AND datetime(?, '+24 hours') "
            "ORDER BY ts",
            (card1, center_ts, center_ts)
        ).fetchall()
        txns = [dict(r) for r in rows]

    # Detect: 3+ online txns under $10 within 60 minutes
    small = [t for t in txns if t['channel'] == 'online'
             and t['TransactionAmt'] is not None
             and float(t['TransactionAmt']) < 10]
    # Find clusters within 60 min
    testing_cluster = []
    for i, t in enumerate(small):
        cluster = [t]
        for j, t2 in enumerate(small):
            if i != j:
                from datetime import datetime
                try:
                    d1 = datetime.fromisoformat(t['ts'])
                    d2 = datetime.fromisoformat(t2['ts'])
                    if abs((d2 - d1).total_seconds()) <= 3600:
                        cluster.append(t2)
                except Exception:
                    pass
        if len(cluster) >= 3 and len(cluster) > len(testing_cluster):
            testing_cluster = cluster

    return {
        "all_txns_in_window": txns,
        "testing_cluster": testing_cluster,
        "has_testing_pattern": len(testing_cluster) >= 3,
    }


def get_similar_closed_cases(customer_id: str = None, card_id: str = None,
                              pattern: str = None, limit: int = 10) -> list[dict]:
    """Retrieve similar historical cases for case memory / GraphRAG."""
    with _con() as con:
        conditions = []
        params = []
        if customer_id:
            conditions.append("customer_id=?")
            params.append(customer_id)
        if card_id:
            conditions.append("card_id=?")
            params.append(card_id)
        if pattern and pattern != 'none':
            conditions.append("pattern=?")
            params.append(pattern)
        where = ("WHERE " + " OR ".join(conditions)) if conditions else ""
        rows = con.execute(
            f"SELECT * FROM closed_cases {where} ORDER BY closed_at DESC LIMIT {limit}",
            params
        ).fetchall()
        return [dict(r) for r in rows]


def get_cases_by_device(device_info: str, before_ts: Optional[str] = None) -> list[dict]:
    """Find closed cases on cards that used this device."""
    if not device_info:
        return []
    with _con() as con:
        rows = con.execute(
            "SELECT DISTINCT cc.* FROM closed_cases cc "
            "JOIN transactions t ON (t.customer_id=cc.customer_id OR t.card1=cc.card_id) "
            "JOIN identity i ON i.TransactionID=t.TransactionID "
            "WHERE i.DeviceInfo LIKE ? LIMIT 20",
            (f"%{device_info.strip()}%",)
        ).fetchall()
        return [dict(r) for r in rows]


def get_cards_sharing_device(device_info: str, exclude_customer: str = None,
                             before_ts: Optional[str] = None) -> list[dict]:
    """Cards from different customers sharing this device — fraud ring signal."""
    if not device_info:
        return []
    with _con() as con:
        params = [f"%{device_info.strip()}%"]
        sql = (
            "SELECT DISTINCT t.customer_id, t.card1, i.DeviceInfo, i.id_15, "
            "MIN(t.ts) as first_seen, MAX(t.ts) as last_seen, COUNT(*) as txn_count "
            "FROM identity i JOIN transactions t ON i.TransactionID=t.TransactionID "
            "WHERE i.DeviceInfo LIKE ?"
        )
        if exclude_customer:
            sql += " AND t.customer_id!=?"
            params.append(exclude_customer)
        if before_ts:
            sql += " AND t.ts<=?"
            params.append(before_ts)
        sql += " GROUP BY t.customer_id, t.card1 ORDER BY last_seen DESC LIMIT 30"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_region_anomaly_cards(addr1: float, window_days: int = 30,
                             before_ts: Optional[str] = None) -> list[dict]:
    """Cards from different customers active in this region recently."""
    with _con() as con:
        params = [addr1]
        sql = (
            "SELECT t.customer_id, t.card1, COUNT(*) as cnt, "
            "SUM(t.TransactionAmt) as total_amt, MAX(t.ts) as last_ts "
            "FROM transactions t WHERE t.addr1=?"
        )
        if before_ts:
            sql += f" AND t.ts BETWEEN datetime(?, '-{window_days} days') AND ?"
            params.extend([before_ts, before_ts])
        sql += " GROUP BY t.customer_id, t.card1 ORDER BY last_ts DESC LIMIT 50"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_customer_email_domains(customer_id: str, before_ts: str = None) -> list[str]:
    """Email domains this customer has used historically."""
    with _con() as con:
        params = [customer_id]
        sql = ("SELECT DISTINCT P_emaildomain FROM transactions "
               "WHERE customer_id=? AND P_emaildomain!=''")
        if before_ts:
            sql += " AND ts<=?"
            params.append(before_ts)
        rows = con.execute(sql, params).fetchall()
        return [r[0] for r in rows if r[0]]


def get_account_behavior_profile(customer_id: str, before_ts: str = None) -> dict:
    """Statistical profile of a customer's normal transaction behavior."""
    with _con() as con:
        params = [customer_id]
        where_time = " AND ts<=?" if before_ts else ""
        if before_ts:
            params.append(before_ts)

        stats = con.execute(
            f"SELECT COUNT(*) as n, AVG(TransactionAmt) as avg_amt, "
            f"MAX(TransactionAmt) as max_amt, MIN(TransactionAmt) as min_amt, "
            f"COUNT(DISTINCT ProductCD) as n_products, "
            f"COUNT(DISTINCT addr1) as n_regions, "
            f"COUNT(DISTINCT channel) as n_channels "
            f"FROM transactions WHERE customer_id=?{where_time}",
            params
        ).fetchone()

        products = con.execute(
            f"SELECT ProductCD, COUNT(*) as cnt FROM transactions "
            f"WHERE customer_id=?{where_time} GROUP BY ProductCD ORDER BY cnt DESC",
            params
        ).fetchall()

        channels = con.execute(
            f"SELECT channel, COUNT(*) as cnt FROM transactions "
            f"WHERE customer_id=?{where_time} GROUP BY channel ORDER BY cnt DESC",
            params
        ).fetchall()

        return {
            "n_txns": stats[0],
            "avg_amount": round(stats[1] or 0, 2),
            "max_amount": stats[2],
            "min_amount": stats[3],
            "n_distinct_products": stats[4],
            "n_distinct_regions": stats[5],
            "n_channels": stats[6],
            "product_distribution": [{"product": r[0], "count": r[1]} for r in products],
            "channel_distribution": [{"channel": r[0], "count": r[1]} for r in channels],
        }


def get_match_flag_anomalies(txn_id: str) -> dict:
    """Check M1-M9 match flags for the transaction — mismatches signal CNP fraud."""
    with _con() as con:
        row = con.execute(
            "SELECT M1,M2,M3,M4,M5,M6,M7,M8,M9 FROM transactions WHERE TransactionID=?",
            (str(txn_id),)
        ).fetchone()
        if not row:
            return {}
        flags = dict(row)
        mismatches = {k: v for k, v in flags.items() if v and 'F' in str(v).upper()}
        matches = {k: v for k, v in flags.items() if v and 'T' in str(v).upper()}
        return {
            "flags": flags,
            "mismatches": mismatches,
            "matches": matches,
            "mismatch_count": len(mismatches),
        }


def write_investigation_case(case: dict) -> str:
    """Write a completed investigation case to the DB (case memory)."""
    import json
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'fraud.db')
    with sqlite3.connect(db_path) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS investigation_cases (
            case_id TEXT PRIMARY KEY,
            customer_id TEXT,
            card_id TEXT,
            status TEXT,
            verdict TEXT,
            fraud_probability REAL,
            pattern TEXT,
            affected_txn_ids TEXT,
            exposure_usd REAL,
            connected_card_ids TEXT,
            summary TEXT,
            created_at TEXT,
            full_json TEXT
        )""")
        con.execute(
            "INSERT OR REPLACE INTO investigation_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?)",
            (
                case.get('case_id'), case.get('customer_id'), case.get('card_id'),
                case.get('status'), case.get('verdict'),
                case.get('fraud_probability'),
                case.get('pattern'),
                json.dumps(case.get('affected_txn_ids', [])),
                case.get('exposure_usd', 0),
                json.dumps(case.get('connected_card_ids', [])),
                case.get('summary', ''),
                json.dumps(case),
            )
        )
        con.commit()
    return case.get('case_id', '')


def get_prior_investigation_cases(customer_id: str = None, pattern: str = None,
                                  limit: int = 5) -> list[dict]:
    """Retrieve previously investigated cases (agent memory from this run)."""
    import json
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'fraud.db')
    try:
        with sqlite3.connect(db_path) as con:
            con.row_factory = sqlite3.Row
            conditions, params = [], []
            if customer_id:
                conditions.append("customer_id=?")
                params.append(customer_id)
            if pattern:
                conditions.append("pattern=?")
                params.append(pattern)
            where = "WHERE " + " OR ".join(conditions) if conditions else ""
            rows = con.execute(
                f"SELECT * FROM investigation_cases {where} "
                f"ORDER BY created_at DESC LIMIT {limit}", params
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []
