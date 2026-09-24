"""
Run the fraud investigation agent on all 20 cases.
Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 run_agent.py [--case HHG-001]  # single case
    python3 run_agent.py                    # all 20 cases
"""

import argparse
import csv
import json
import os
import sys

# Add parent dir so imports work when run from any cwd
sys.path.insert(0, os.path.dirname(__file__))

from agent.investigator import FraudInvestigator

DATA_DIR = os.path.join(os.path.dirname(__file__), '..')
CASES_DIR = os.path.join(os.path.dirname(__file__), 'cases')
PACK_CSV = os.path.join(DATA_DIR, 'case_pack.csv')
DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'fraud.db')


def load_case_pack():
    with open(PACK_CSV, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    for row in rows:
        row['risk_score'] = float(row['risk_score']) if row.get('risk_score') else None
    return rows


def ensure_db():
    if not os.path.exists(DB_PATH):
        print("Database not found. Building it now (this takes ~3 minutes)...")
        import subprocess
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), 'data', 'build_db.py')],
            capture_output=False
        )
        if result.returncode != 0:
            print("ERROR: Failed to build database. Run: python3 data/build_db.py")
            sys.exit(1)


def run_case(investigator: FraudInvestigator, case: dict) -> dict:
    try:
        result = investigator.investigate(case)
    except Exception as e:
        import traceback
        print(f"ERROR on {case['case_id']}: {e}")
        traceback.print_exc()
        result = {
            "case_id": case['case_id'],
            "error": str(e),
        }
    return result


def save_case(result: dict):
    os.makedirs(CASES_DIR, exist_ok=True)
    path = os.path.join(CASES_DIR, f"{result['case_id']}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', help='Run a single case ID, e.g. HHG-001')
    parser.add_argument('--skip-existing', action='store_true',
                        help='Skip cases that already have output files')
    args = parser.parse_args()

    # Load .env if present
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        for line in open(env_file).read().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

    if not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('OPENAI_API_KEY'):
        print("ERROR: Set ANTHROPIC_API_KEY or OPENAI_API_KEY in fraud_agent/.env")
        sys.exit(1)

    ensure_db()

    cases = load_case_pack()
    if args.case:
        cases = [c for c in cases if c['case_id'] == args.case]
        if not cases:
            print(f"Case {args.case} not found in case_pack.csv")
            sys.exit(1)

    if args.skip_existing:
        cases = [c for c in cases
                 if not os.path.exists(os.path.join(CASES_DIR, f"{c['case_id']}.json"))]
        print(f"Skipping existing — {len(cases)} cases to run")

    print(f"\nRunning {len(cases)} investigation(s)...")
    investigator = FraudInvestigator()

    summary = []
    for case in cases:
        result = run_case(investigator, case)
        save_case(result)
        if 'case' in result:
            summary.append({
                "case_id": result['case_id'],
                "verdict": result['case']['verdict'],
                "pattern": result['case']['pattern'],
                "probability": result['case']['fraud_probability'],
                "exposure": result['case']['exposure_usd'],
                "status": result['case']['status'],
            })

    print(f"\n{'='*60}")
    print(f"SUMMARY — {len(summary)} cases completed")
    print(f"{'='*60}")
    for s in summary:
        print(f"  {s['case_id']}: {s['verdict']:12s} {s['pattern']:35s} p={s['probability']:.2f} ${s['exposure']:.2f}")

    fraud_count = sum(1 for s in summary if s['verdict'] == 'fraud')
    legit_count = sum(1 for s in summary if s['verdict'] == 'legitimate')
    uncertain_count = sum(1 for s in summary if s['verdict'] == 'uncertain')
    print(f"\n  Fraud: {fraud_count} | Legitimate: {legit_count} | Uncertain: {uncertain_count}")
    print(f"  Output: {CASES_DIR}/")


if __name__ == '__main__':
    main()
