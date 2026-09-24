"""
Build the local SQLite investigation database from the IEEE-CIS CSV files.
This is the graph-backed data store the agent queries. On top of SQLite we
simulate the TigerGraph graph structure so the agent logic runs identically
whether talking to TigerGraph MCP or this local store.

Run once:  python3 build_db.py
"""

import csv
import sqlite3
import os
import sys
import time

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..')
# Go up to HHGOA_IEEE directory
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
DB_PATH = os.path.join(os.path.dirname(__file__), 'fraud.db')

TXN_CSV = os.path.join(DATA_DIR, 'transactions.csv')
ID_CSV = os.path.join(DATA_DIR, 'identity.csv')
CASES_CSV = os.path.join(DATA_DIR, 'closed_cases_history.csv')
PACK_CSV = os.path.join(DATA_DIR, 'case_pack.csv')


def connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-64000")  # 64MB cache
    return con


def create_schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS transactions (
        TransactionID TEXT PRIMARY KEY,
        TransactionDT INTEGER,
        TransactionAmt REAL,
        ProductCD TEXT,
        card1 TEXT, card2 REAL, card3 REAL, card4 TEXT, card5 REAL, card6 TEXT,
        addr1 REAL, addr2 REAL, dist1 REAL, dist2 REAL,
        P_emaildomain TEXT, R_emaildomain TEXT,
        C1 REAL, C2 REAL, C3 REAL, C4 REAL, C5 REAL, C6 REAL,
        C7 REAL, C8 REAL, C9 REAL, C10 REAL, C11 REAL, C12 REAL, C13 REAL, C14 REAL,
        D1 REAL, D2 REAL, D3 REAL, D4 REAL, D5 REAL,
        M1 TEXT, M2 TEXT, M3 TEXT, M4 TEXT, M5 TEXT, M6 TEXT, M7 TEXT, M8 TEXT, M9 TEXT,
        V1 REAL, V2 REAL, V3 REAL, V4 REAL, V5 REAL, V6 REAL,
        V45 REAL, V46 REAL, V47 REAL, V48 REAL, V49 REAL, V50 REAL, V51 REAL, V52 REAL,
        V54 REAL, V55 REAL, V56 REAL, V57 REAL, V58 REAL, V59 REAL, V60 REAL, V61 REAL,
        V62 REAL, V63 REAL, V64 REAL, V65 REAL, V66 REAL, V67 REAL, V68 REAL, V69 REAL, V70 REAL,
        customer_id TEXT,
        ts TEXT,
        channel TEXT,
        risk_score REAL
    );

    CREATE TABLE IF NOT EXISTS identity (
        TransactionID TEXT PRIMARY KEY,
        id_01 REAL, id_02 REAL, id_03 REAL, id_04 REAL, id_05 REAL,
        id_06 REAL, id_07 REAL, id_08 REAL, id_09 REAL, id_10 REAL, id_11 REAL,
        id_12 TEXT, id_13 TEXT, id_14 TEXT, id_15 TEXT, id_16 TEXT,
        id_17 TEXT, id_18 TEXT, id_19 TEXT, id_20 TEXT, id_21 TEXT, id_22 TEXT, id_23 TEXT,
        id_24 TEXT, id_25 TEXT, id_26 TEXT, id_27 TEXT, id_28 TEXT, id_29 TEXT, id_30 TEXT,
        id_31 TEXT, id_32 TEXT, id_33 TEXT, id_34 TEXT, id_35 TEXT, id_36 TEXT, id_37 TEXT, id_38 TEXT,
        DeviceType TEXT,
        DeviceInfo TEXT
    );

    CREATE TABLE IF NOT EXISTS closed_cases (
        case_id TEXT PRIMARY KEY,
        customer_id TEXT,
        card_id TEXT,
        opened_at TEXT,
        closed_at TEXT,
        outcome TEXT,
        pattern TEXT,
        first_fraud_txn_id TEXT,
        txn_ids TEXT,
        n_txns INTEGER,
        exposure_usd REAL,
        connected_card_ids TEXT,
        actions_taken TEXT,
        report_filed TEXT,
        analyst_notes TEXT
    );

    CREATE TABLE IF NOT EXISTS case_pack (
        case_id TEXT PRIMARY KEY,
        opened_at TEXT,
        trigger_type TEXT,
        trigger_text TEXT,
        flagged_txn_id TEXT,
        card_id TEXT,
        customer_id TEXT,
        risk_score REAL
    );

    CREATE INDEX IF NOT EXISTS idx_txn_customer ON transactions(customer_id);
    CREATE INDEX IF NOT EXISTS idx_txn_card1 ON transactions(card1);
    CREATE INDEX IF NOT EXISTS idx_txn_ts ON transactions(ts);
    CREATE INDEX IF NOT EXISTS idx_txn_addr1 ON transactions(addr1);
    CREATE INDEX IF NOT EXISTS idx_txn_channel ON transactions(channel);
    CREATE INDEX IF NOT EXISTS idx_identity_txn ON identity(TransactionID);
    CREATE INDEX IF NOT EXISTS idx_cc_customer ON closed_cases(customer_id);
    CREATE INDEX IF NOT EXISTS idx_cc_card ON closed_cases(card_id);
    CREATE INDEX IF NOT EXISTS idx_cc_pattern ON closed_cases(pattern);
    """)
    con.commit()


def safe_float(v):
    try:
        return float(v) if v and v.strip() else None
    except Exception:
        return None


def safe_int(v):
    try:
        return int(v) if v and v.strip() else None
    except Exception:
        return None


def load_transactions(con):
    print("Loading transactions.csv ...", flush=True)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM transactions")
    if cur.fetchone()[0] > 0:
        print("  Already loaded, skipping.")
        return

    batch = []
    BATCH = 5000
    cols_needed = [
        'TransactionID', 'TransactionDT', 'TransactionAmt', 'ProductCD',
        'card1', 'card2', 'card3', 'card4', 'card5', 'card6',
        'addr1', 'addr2', 'dist1', 'dist2', 'P_emaildomain', 'R_emaildomain',
        'C1','C2','C3','C4','C5','C6','C7','C8','C9','C10','C11','C12','C13','C14',
        'D1','D2','D3','D4','D5',
        'M1','M2','M3','M4','M5','M6','M7','M8','M9',
        'V1','V2','V3','V4','V5','V6',
        'V45','V46','V47','V48','V49','V50','V51','V52',
        'V54','V55','V56','V57','V58','V59','V60','V61',
        'V62','V63','V64','V65','V66','V67','V68','V69','V70',
        'customer_id', 'ts', 'channel', 'risk_score'
    ]

    with open(TXN_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            r = row  # dict
            batch.append((
                r.get('TransactionID'),
                safe_int(r.get('TransactionDT')),
                safe_float(r.get('TransactionAmt')),
                r.get('ProductCD'),
                r.get('card1'), safe_float(r.get('card2')), safe_float(r.get('card3')),
                r.get('card4'), safe_float(r.get('card5')), r.get('card6'),
                safe_float(r.get('addr1')), safe_float(r.get('addr2')),
                safe_float(r.get('dist1')), safe_float(r.get('dist2')),
                r.get('P_emaildomain'), r.get('R_emaildomain'),
                safe_float(r.get('C1')), safe_float(r.get('C2')), safe_float(r.get('C3')),
                safe_float(r.get('C4')), safe_float(r.get('C5')), safe_float(r.get('C6')),
                safe_float(r.get('C7')), safe_float(r.get('C8')), safe_float(r.get('C9')),
                safe_float(r.get('C10')), safe_float(r.get('C11')), safe_float(r.get('C12')),
                safe_float(r.get('C13')), safe_float(r.get('C14')),
                safe_float(r.get('D1')), safe_float(r.get('D2')), safe_float(r.get('D3')),
                safe_float(r.get('D4')), safe_float(r.get('D5')),
                r.get('M1'), r.get('M2'), r.get('M3'), r.get('M4'), r.get('M5'),
                r.get('M6'), r.get('M7'), r.get('M8'), r.get('M9'),
                safe_float(r.get('V1')), safe_float(r.get('V2')), safe_float(r.get('V3')),
                safe_float(r.get('V4')), safe_float(r.get('V5')), safe_float(r.get('V6')),
                safe_float(r.get('V45')), safe_float(r.get('V46')), safe_float(r.get('V47')),
                safe_float(r.get('V48')), safe_float(r.get('V49')), safe_float(r.get('V50')),
                safe_float(r.get('V51')), safe_float(r.get('V52')),
                safe_float(r.get('V54')), safe_float(r.get('V55')), safe_float(r.get('V56')),
                safe_float(r.get('V57')), safe_float(r.get('V58')), safe_float(r.get('V59')),
                safe_float(r.get('V60')), safe_float(r.get('V61')),
                safe_float(r.get('V62')), safe_float(r.get('V63')), safe_float(r.get('V64')),
                safe_float(r.get('V65')), safe_float(r.get('V66')), safe_float(r.get('V67')),
                safe_float(r.get('V68')), safe_float(r.get('V69')), safe_float(r.get('V70')),
                r.get('customer_id'), r.get('ts'), r.get('channel'),
                safe_float(r.get('risk_score')),
            ))
            count += 1
            if len(batch) >= BATCH:
                con.executemany(
                    "INSERT OR IGNORE INTO transactions VALUES "
                    "(" + ",".join(["?"] * 79) + ")", batch
                )
                con.commit()
                batch = []
                if count % 100000 == 0:
                    print(f"  {count:,} rows ...", flush=True)

        if batch:
            con.executemany(
                "INSERT OR IGNORE INTO transactions VALUES "
                "(" + ",".join(["?"] * 79) + ")", batch
            )
            con.commit()
    print(f"  Done: {count:,} transactions loaded.")


def load_identity(con):
    print("Loading identity.csv ...", flush=True)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM identity")
    if cur.fetchone()[0] > 0:
        print("  Already loaded, skipping.")
        return

    batch = []
    BATCH = 5000
    with open(ID_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            r = row
            batch.append((
                r.get('TransactionID'),
                safe_float(r.get('id_01')), safe_float(r.get('id_02')),
                safe_float(r.get('id_03')), safe_float(r.get('id_04')),
                safe_float(r.get('id_05')), safe_float(r.get('id_06')),
                safe_float(r.get('id_07')), safe_float(r.get('id_08')),
                safe_float(r.get('id_09')), safe_float(r.get('id_10')),
                safe_float(r.get('id_11')),
                r.get('id_12'), r.get('id_13'), r.get('id_14'), r.get('id_15'),
                r.get('id_16'), r.get('id_17'), r.get('id_18'), r.get('id_19'),
                r.get('id_20'), r.get('id_21'), r.get('id_22'), r.get('id_23'),
                r.get('id_24'), r.get('id_25'), r.get('id_26'), r.get('id_27'),
                r.get('id_28'), r.get('id_29'), r.get('id_30'), r.get('id_31'),
                r.get('id_32'), r.get('id_33'), r.get('id_34'), r.get('id_35'),
                r.get('id_36'), r.get('id_37'), r.get('id_38'),
                r.get('DeviceType'), r.get('DeviceInfo'),
            ))
            count += 1
            if len(batch) >= BATCH:
                con.executemany(
                    "INSERT OR IGNORE INTO identity VALUES "
                    "(" + ",".join(["?"] * 41) + ")", batch
                )
                con.commit()
                batch = []

        if batch:
            con.executemany(
                "INSERT OR IGNORE INTO identity VALUES "
                "(" + ",".join(["?"] * 41) + ")", batch
            )
            con.commit()
    print(f"  Done: {count:,} identity records loaded.")


def load_closed_cases(con):
    print("Loading closed_cases_history.csv ...", flush=True)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM closed_cases")
    if cur.fetchone()[0] > 0:
        print("  Already loaded, skipping.")
        return

    with open(CASES_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append((
                row['case_id'], row['customer_id'], row['card_id'],
                row['opened_at'], row['closed_at'], row['outcome'], row['pattern'],
                row['first_fraud_txn_id'], row['txn_ids'],
                safe_int(row['n_txns']), safe_float(row['exposure_usd']),
                row['connected_card_ids'], row['actions_taken'],
                row['report_filed'], row['analyst_notes'],
            ))
        con.executemany("INSERT OR IGNORE INTO closed_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    print(f"  Done: {len(rows)} closed cases loaded.")


def load_case_pack(con):
    print("Loading case_pack.csv ...", flush=True)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM case_pack")
    if cur.fetchone()[0] > 0:
        print("  Already loaded, skipping.")
        return

    with open(PACK_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append((
                row['case_id'], row['opened_at'], row['trigger_type'],
                row['trigger_text'], row['flagged_txn_id'], row['card_id'],
                row['customer_id'], safe_float(row['risk_score']),
            ))
        con.executemany("INSERT OR IGNORE INTO case_pack VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
    print(f"  Done: {len(rows)} exam cases loaded.")


def main():
    t0 = time.time()
    if os.path.exists(DB_PATH):
        print(f"Database exists at {DB_PATH}")
    else:
        print(f"Creating database at {DB_PATH}")

    con = connect()
    create_schema(con)
    load_transactions(con)
    load_identity(con)
    load_closed_cases(con)
    load_case_pack(con)
    con.close()
    print(f"\nDone in {time.time()-t0:.1f}s")


if __name__ == '__main__':
    main()
