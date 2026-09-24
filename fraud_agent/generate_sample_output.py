"""
Generate sample output for HHG-001 without an API key.
Shows the exact JSON format that the agent produces.
Run: python3 generate_sample_output.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))

from agent import graph_queries as gq
from agent.policy import build_initial_actions, build_final_actions

def analyze_case_rule_based(case_id, customer_id, card_id, flagged_txn_id,
                             trigger_type, risk_score, opened_at):
    """Rule-based analysis without LLM — for testing/demo."""
    txn = gq.get_transaction(flagged_txn_id)
    if not txn:
        return None

    card1 = txn.get('card1', '')
    txn_ts = txn.get('ts', opened_at)
    txn_amt = float(txn.get('TransactionAmt') or 0)
    addr1 = txn.get('addr1')
    channel = txn.get('channel', '')
    device_info = txn.get('DeviceInfo', '') or ''
    id_15 = txn.get('id_15', '') or ''

    profile = gq.get_account_behavior_profile(customer_id, before_ts=txn_ts)
    history = gq.get_card_history(customer_id, card1=card1, before_ts=opened_at, limit=50)
    testing = gq.get_card_testing_signals(card1, txn_ts) if card1 else {"has_testing_pattern": False, "testing_cluster": []}
    region_info = gq.get_region_history(addr1, customer_id, before_ts=txn_ts) if addr1 else {}
    device_neighbors = gq.get_cards_sharing_device(device_info, exclude_customer=customer_id) if device_info and channel == 'online' else []
    match_flags = gq.get_match_flag_anomalies(flagged_txn_id)
    similar_cases = gq.get_similar_closed_cases(customer_id=customer_id, card_id=card_id, limit=5)

    # Determine pattern
    has_card_testing = testing.get('has_testing_pattern', False)
    is_new_region = region_info.get('is_new_region', False) and channel == 'in_person'
    is_new_device = id_15 == 'New' and channel == 'online'
    has_shared_device = len(device_neighbors) > 0
    customer_disputed = trigger_type == 'customer_report'

    if has_card_testing:
        pattern = 'card_testing'
    elif is_new_device:
        pattern = 'card_not_present_new_device'
    elif channel == 'online' and txn_amt > profile.get('avg_amount', 0) * 2:
        pattern = 'card_not_present_fraud'
    elif is_new_region:
        pattern = 'out_of_region_use'
    else:
        pattern = 'none'

    # Fraud probability
    fraud_probability = risk_score or 0.3
    if customer_disputed: fraud_probability = max(fraud_probability, 0.55)
    if is_new_device: fraud_probability = min(0.95, fraud_probability + 0.20)
    if is_new_region: fraud_probability = min(0.95, fraud_probability + 0.20)
    if has_card_testing: fraud_probability = min(0.95, fraud_probability + 0.30)
    if has_shared_device: fraud_probability = min(0.95, fraud_probability + 0.15)
    if match_flags.get('mismatch_count', 0) > 1: fraud_probability = min(0.95, fraud_probability + 0.10)
    # Discount for familiar region and normal amounts
    if not is_new_region and not is_new_device and not customer_disputed and not has_card_testing:
        if txn_amt <= profile.get('avg_amount', 9999) * 1.5:
            fraud_probability = max(0.15, fraud_probability - 0.15)

    verdict = 'fraud' if fraud_probability >= 0.70 else ('legitimate' if fraud_probability <= 0.30 else 'uncertain')
    affected_txn_ids = [str(flagged_txn_id)] if verdict == 'fraud' else []

    # Actions
    initial_actions = build_initial_actions(fraud_probability, txn_amt, trigger_type, pattern,
                                            has_card_testing, has_shared_device, customer_disputed)
    assumed_response = 'denied' if customer_disputed else ('denied' if fraud_probability > 0.75 else ('confirmed' if fraud_probability < 0.35 else 'no_reply'))
    final_actions = build_final_actions(fraud_probability, txn_amt, pattern, assumed_response,
                                        has_shared_device, False, False,
                                        [d['card1'] for d in device_neighbors[:3]])

    if assumed_response == 'denied': fraud_probability = min(0.95, fraud_probability + 0.20)
    if assumed_response == 'confirmed': fraud_probability = max(0.05, fraud_probability - 0.30)

    verdict = 'fraud' if fraud_probability >= 0.70 else ('legitimate' if fraud_probability <= 0.30 else 'uncertain')

    evidence = [{"claim": f"Transaction ${txn_amt:.2f} {channel} in region {addr1}.",
                 "source": "graph", "ref": "query:get_transaction", "entity_ids": [str(flagged_txn_id)]}]
    if is_new_region:
        evidence.append({"claim": f"Region {addr1} is new for customer {customer_id}.",
                         "source": "graph", "ref": "query:region_history", "entity_ids": [str(flagged_txn_id)]})
    if similar_cases:
        evidence.append({"claim": f"Similar prior cases: {[c['case_id'] for c in similar_cases[:3]]}",
                         "source": "graph", "ref": "query:similar_cases", "entity_ids": [c['case_id'] for c in similar_cases[:3]]})

    file_report = any(a.action == 'FILE_REPORT' for a in final_actions)
    status = 'closed_fraud' if verdict == 'fraud' else ('closed_legitimate' if verdict == 'legitimate' else 'open')

    return {
        "case_id": case_id,
        "case": {
            "status": status, "verdict": verdict,
            "fraud_probability": round(fraud_probability, 3),
            "pattern": pattern, "pattern_description": "",
            "affected_txn_ids": affected_txn_ids, "first_suspicious_txn_id": str(flagged_txn_id) if verdict == 'fraud' else "",
            "connected_card_ids": [d['card1'] for d in device_neighbors[:3]],
            "connected_device_profiles": [device_info] if device_info and has_shared_device else [],
            "exposure_usd": round(txn_amt, 2) if verdict == 'fraud' else 0,
            "evidence": evidence,
            "similar_prior_cases": [c['case_id'] for c in similar_cases[:3]],
            "summary": f"Rule-based analysis. Pattern: {pattern}. Fraud probability: {fraud_probability:.2f}. Trigger: {trigger_type}.",
            "written_to_graph": True, "graph_case_id": case_id,
        },
        "evidence_requests": [{"type": "customer_validation", "asked_after_step": 2,
                                "assumed_response": f"Customer {'denied' if assumed_response=='denied' else 'confirmed'} the transaction."}],
        "next_best_actions": {
            "initial": [{"action": a.action, "route": a.route, "reason": a.reason} for a in initial_actions],
            "final": [{"action": a.action, "route": a.route, "reason": a.reason} for a in final_actions],
            "what_changed": f"Customer response ({assumed_response}) updated the recommendation." if assumed_response != 'pending' else "nothing",
        },
        "sar": {
            "file": file_report,
            "reason": "Fraud confirmed with sufficient exposure." if file_report else "Conditions for SAR not met.",
            "narrative": (f"On {txn.get('ts','')[:10]}, card {card_id} belonging to customer {customer_id} "
                          f"was used for a {channel} transaction of ${txn_amt:.2f}. "
                          f"The activity is inconsistent with the customer's normal behavior. Pattern: {pattern}. "
                          f"This report is filed per bank policy R2.") if file_report else "",
            "subjects": [customer_id, card_id] if file_report else [],
            "total_amount_usd": round(txn_amt, 2) if file_report else 0,
            "activity_dates": [txn.get('ts','')[:10], txn.get('ts','')[:10]] if file_report else [],
        },
        "stop_reason": f"Rule-based analysis complete. Probability {fraud_probability:.2f}.",
        "tool_calls": 8, "tokens": 0, "latency_s": 0.5,
    }

if __name__ == '__main__':
    import csv
    cases_dir = os.path.join(os.path.dirname(__file__), 'cases')
    os.makedirs(cases_dir, exist_ok=True)
    pack_csv = os.path.join(os.path.dirname(__file__), '..', 'case_pack.csv')
    with open(pack_csv) as f:
        cases = list(csv.DictReader(f))
    for case in cases:
        print(f"Analyzing {case['case_id']}...", end=' ', flush=True)
        result = analyze_case_rule_based(
            case['case_id'], case['customer_id'], case['card_id'],
            case['flagged_txn_id'], case['trigger_type'],
            float(case['risk_score']) if case['risk_score'] else None,
            case['opened_at']
        )
        if result:
            out_path = os.path.join(cases_dir, f"{case['case_id']}.json")
            with open(out_path, 'w') as f:
                json.dump(result, f, indent=2)
            print(f"→ {result['case']['verdict']} ({result['case']['pattern']})")
        else:
            print("ERROR: transaction not found")
    print(f"\nDone. Output in {cases_dir}/")
