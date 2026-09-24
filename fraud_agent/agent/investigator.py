"""
Core fraud investigation agent.
LLM: OpenAI o3 (OPENAI_API_KEY).
TigerGraph: writes cases to TigerGraph when TG_HOST is configured via MCP.
Graph evidence: always uses local SQLite (mirrors TigerGraph schema).
Produces a complete answer JSON per case matching the README spec exactly.
"""

import json
import os
import time
from typing import Optional
from pathlib import Path

# Load .env from the fraud_agent directory if present
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from . import graph_queries as gq
from .policy import (
    build_initial_actions, build_final_actions, should_stop,
    should_open_case, should_file_report, Action
)
from . import mcp_tools as tg_mcp

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


def _make_llm_client():
    """Return (client, provider)."""
    if OPENAI_API_KEY:
        from openai import OpenAI
        return OpenAI(api_key=OPENAI_API_KEY), "openai"
    if ANTHROPIC_API_KEY:
        import anthropic
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY), "anthropic"
    raise RuntimeError(
        "No LLM API key found. Set OPENAI_API_KEY in fraud_agent/.env"
    )

FRAUD_POLICY = """
FRAUD POLICY SUMMARY (Operational rules):
R1. Verify before blocking on weak signal (fraud prob < 0.70, single signal): VERIFY_WITH_CUSTOMER or STEP_UP_AUTH first.
R2. Customer denies → BLOCK_CARD + CREATE_CASE. Add FILE_REPORT if exposure > $1,000 or shared device.
R3. Customer confirms → CLOSE_NO_FRAUD.
R4. No reply 24h → MONITOR_CARD + DECLINE_TRANSACTION. Escalate if exposure > $500.
R5. Card testing (3+ sub-$10 online in 1hr then larger purchase) → DECLINE_TRANSACTION + STEP_UP_AUTH. If purchase >$100 cleared → BLOCK_CARD.
R6. Shared device/region across cards → CREATE_CASE + FILE_REPORT + MONITOR_CONNECTED_CARDS.
R7. Disputed recurring pattern → CREATE_CASE + VERIFY_WITH_CUSTOMER + WARN_CUSTOMER, no block.
R8. Uncertain (0.40–0.60) + exposure > $500 → ESCALATE_TO_ANALYST.
R9. Undocumented coordinated pattern → CREATE_CASE + FILE_REPORT + ESCALATE_TO_ANALYST.
R10. BLOCK_ALL_CARDS only when 2+ confirmed compromised cards.

PATTERNS: card_testing | card_not_present_fraud | card_not_present_new_device | out_of_region_use | account_takeover | undocumented | none
APPROVAL: auto (most actions) | L1 (DECLINE_TRANSACTION, BLOCK_CARD ≤$2,500) | L2 (BLOCK_CARD >$2,500, BLOCK_ALL_CARDS, FILE_REPORT)
"""

KNOWN_PATTERNS = """
KNOWN FRAUD PATTERNS:
1. card_testing: 3+ small online auths under $5 within 1 hour, then larger purchase. Confirmed by sequence.
2. card_not_present_fraud: Card number used online without card. Burst of 2-4 txns in 48h, amounts/products don't fit history.
3. card_not_present_new_device: Same as #2 but identity record shows device as 'New' for account, sometimes behind proxy.
4. out_of_region_use: Card-present purchases (in_person, ProductCD=W) in region the customer has no history in.
5. account_takeover: Mixed-channel activity inconsistent with cardholder, device/match-flag anomalies, stolen credentials.
"""


def _fmt_actions(actions: list[Action]) -> list[dict]:
    return [{"action": a.action, "route": a.route, "reason": a.reason} for a in actions]


class FraudInvestigator:
    def __init__(self):
        self.client, self.provider = _make_llm_client()
        self.tool_calls = 0
        self.tokens = 0
        print(f"[Agent] LLM provider: {self.provider}")
        print(f"[Agent] TigerGraph MCP: {'available' if tg_mcp.is_available() else 'not configured (set TG_HOST in .env)'}")

    def _llm(self, system: str, messages: list[dict], max_tokens: int = 4096) -> str:
        if self.provider == "anthropic":
            response = self.client.messages.create(
                model="claude-opus-4-7",
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
            self.tokens += response.usage.input_tokens + response.usage.output_tokens
            return response.content[0].text
        else:
            # OpenAI o3 uses max_completion_tokens, not max_tokens
            resp = self.client.chat.completions.create(
                model="o3",
                max_completion_tokens=max_tokens,
                messages=[{"role": "system", "content": system}] + messages,
            )
            self.tokens += resp.usage.total_tokens
            return resp.choices[0].message.content

    def investigate(self, case_pack_row: dict) -> dict:
        t0 = time.time()
        self.tool_calls = 0
        self.tokens = 0

        case_id = case_pack_row['case_id']
        customer_id = case_pack_row['customer_id']
        card_id = case_pack_row['card_id']
        flagged_txn_id = str(case_pack_row['flagged_txn_id'])
        trigger_type = case_pack_row['trigger_type']
        trigger_text = case_pack_row['trigger_text']
        risk_score = case_pack_row.get('risk_score') or 0.0
        opened_at = case_pack_row['opened_at']

        print(f"\n{'='*60}")
        print(f"Investigating {case_id} | Customer {customer_id} | Card {card_id}")
        print(f"Trigger: {trigger_type} | Flagged: {flagged_txn_id}")

        # ── Step 1: Fetch the flagged transaction ──
        flagged_txn = gq.get_transaction(flagged_txn_id)
        self.tool_calls += 1
        if not flagged_txn:
            return self._error_case(case_id, customer_id, card_id, "Flagged transaction not found")

        card1 = flagged_txn.get('card1', '')
        txn_ts = flagged_txn.get('ts', opened_at)
        txn_amt = float(flagged_txn.get('TransactionAmt') or 0)
        addr1 = flagged_txn.get('addr1')
        channel = flagged_txn.get('channel', '')
        device_info = flagged_txn.get('DeviceInfo', '') or ''
        device_type = flagged_txn.get('DeviceType', '') or ''
        id_15 = flagged_txn.get('id_15', '') or ''  # New/Found
        id_23 = flagged_txn.get('id_23', '') or ''  # proxy
        id_30 = flagged_txn.get('id_30', '') or ''  # OS
        id_31 = flagged_txn.get('id_31', '') or ''  # browser

        print(f"  Amount: ${txn_amt:.2f} | Channel: {channel} | addr1: {addr1}")
        print(f"  Device: {device_info[:60] if device_info else 'none'} | id_15: {id_15}")

        # ── Step 2: Customer behavior profile ──
        profile = gq.get_account_behavior_profile(customer_id, before_ts=txn_ts)
        self.tool_calls += 1

        # ── Step 3: Card history (recent 100 transactions) ──
        history = gq.get_card_history(customer_id, card1=card1, before_ts=opened_at, limit=100)
        self.tool_calls += 1

        # ── Step 4: Transaction window (48h around flagged) ──
        window = gq.get_transaction_window(card1, txn_ts, hours=48) if card1 else []
        self.tool_calls += 1

        # ── Step 5: Card testing check ──
        testing = gq.get_card_testing_signals(card1, txn_ts) if card1 else {"has_testing_pattern": False, "testing_cluster": [], "all_txns_in_window": []}
        self.tool_calls += 1

        # ── Step 6: Region analysis ──
        region_info = {}
        if addr1:
            region_info = gq.get_region_history(addr1, customer_id, before_ts=txn_ts)
            self.tool_calls += 1

        # ── Step 7: Device neighbors ──
        device_neighbors = []
        device_cases = []
        connected_card_ids_from_device = []
        if device_info and channel == 'online':
            device_neighbors = gq.get_cards_sharing_device(device_info, exclude_customer=customer_id, before_ts=opened_at)
            device_cases = gq.get_cases_by_device(device_info, before_ts=opened_at)
            self.tool_calls += 2
            connected_card_ids_from_device = list({r['card1'] for r in device_neighbors if r['card1'] != card1})

        # ── Step 8: Match flag anomalies ──
        match_flags = gq.get_match_flag_anomalies(flagged_txn_id)
        self.tool_calls += 1

        # ── Step 9: Similar closed cases (memory) ──
        similar_cases = gq.get_similar_closed_cases(
            customer_id=customer_id, card_id=card_id, limit=8
        )
        self.tool_calls += 1

        # ── Step 10: Prior investigation cases from this run ──
        prior_investigations = gq.get_prior_investigation_cases(customer_id=customer_id, limit=3)

        # ── Step 11: Email domain history ──
        historical_domains = gq.get_customer_email_domains(customer_id, before_ts=txn_ts)
        current_domain = flagged_txn.get('P_emaildomain', '') or ''
        domain_is_new = current_domain and current_domain not in historical_domains
        self.tool_calls += 1

        # ── Step 12: Assemble evidence context for LLM ──
        evidence_context = self._build_evidence_context(
            flagged_txn, profile, history, window, testing, region_info,
            device_neighbors, device_cases, match_flags, similar_cases,
            historical_domains, current_domain, domain_is_new,
            trigger_type, trigger_text, risk_score, card_id
        )

        # ── Step 13: LLM investigation analysis ──
        analysis = self._llm_analyze(evidence_context, case_id, customer_id, card_id,
                                     flagged_txn_id, trigger_type, risk_score)

        # ── Step 14: Parse analysis ──
        parsed = self._parse_analysis(analysis)
        fraud_probability = parsed.get('fraud_probability', 0.5)
        pattern = parsed.get('pattern', 'none')
        affected_txn_ids = parsed.get('affected_txn_ids', [flagged_txn_id])
        evidence_list = parsed.get('evidence', [])
        summary = parsed.get('summary', '')
        pattern_description = parsed.get('pattern_description', '')
        first_suspicious_txn_id = parsed.get('first_suspicious_txn_id', '')
        verdict = parsed.get('verdict', 'uncertain')

        # ── Step 15: Compute exposure ──
        exposure = self._compute_exposure(affected_txn_ids)
        self.tool_calls += 1

        # ── Step 16: Determine if connected cards ──
        connected_card_ids = list(set(
            parsed.get('connected_card_ids', []) + connected_card_ids_from_device[:5]
        ))

        has_card_testing = testing.get('has_testing_pattern', False)
        has_shared_device = len(device_neighbors) > 0 or len(device_cases) > 0
        customer_disputed = trigger_type == 'customer_report'
        is_coordinated = pattern == 'undocumented' and len(connected_card_ids) > 0

        # ── Step 17: Initial actions ──
        initial_actions = build_initial_actions(
            fraud_probability=fraud_probability,
            exposure=exposure,
            trigger_type=trigger_type,
            pattern=pattern,
            has_card_testing=has_card_testing,
            has_shared_device=has_shared_device,
            customer_disputed=customer_disputed,
        )

        # ── Step 18: Evidence request simulation ──
        evidence_requests = []
        needs_verification = any(a.action in ('VERIFY_WITH_CUSTOMER', 'STEP_UP_AUTH')
                                  for a in initial_actions)

        assumed_response = 'pending'
        if needs_verification:
            assumed_response, req = self._simulate_evidence_request(
                fraud_probability, trigger_type, pattern, exposure
            )
            evidence_requests.append(req)

        # ── Step 19: Final actions (after assumed response) ──
        final_actions = build_final_actions(
            fraud_probability=fraud_probability,
            exposure=exposure,
            pattern=pattern,
            customer_response=assumed_response,
            has_shared_device=has_shared_device,
            has_connected_fraud=len(device_cases) > 0,
            is_coordinated=is_coordinated,
            connected_card_ids=connected_card_ids,
        )

        # Update fraud probability based on assumed response
        if assumed_response == 'denied':
            fraud_probability = min(0.95, fraud_probability + 0.20)
            verdict = 'fraud'
        elif assumed_response == 'confirmed':
            fraud_probability = max(0.05, fraud_probability - 0.30)
            verdict = 'legitimate'

        # ── Step 20: Determine status ──
        should_escalate = any(a.action == 'ESCALATE_TO_ANALYST' for a in final_actions)
        if verdict == 'fraud':
            status = 'closed_fraud'
        elif verdict == 'legitimate':
            status = 'closed_legitimate'
        elif should_escalate:
            status = 'escalated'
        elif verdict == 'uncertain' and assumed_response == 'pending':
            status = 'open'
        else:
            status = 'open'

        # ── Step 21: Stop reason ──
        stopped, stop_reason = should_stop(
            fraud_probability, len(evidence_list),
            assumed_response in ('denied', 'confirmed')
        )
        if not stop_reason:
            if assumed_response == 'denied':
                stop_reason = "Customer denied the transaction; verdict settled. Card action required."
            elif assumed_response == 'confirmed':
                stop_reason = "Customer confirmed the transaction; closed as legitimate."
            elif assumed_response == 'no_reply':
                stop_reason = "No customer reply within 24h; monitoring applied, escalated if exposed."
            else:
                stop_reason = f"Investigation complete. Fraud probability {fraud_probability:.2f}. Further steps unlikely to change decision."

        # ── Step 22: SAR ──
        file_report = any(a.action == 'FILE_REPORT' for a in final_actions)
        sar = self._build_sar(
            file_report, fraud_probability, exposure, pattern,
            customer_id, card_id, affected_txn_ids, connected_card_ids,
            txn_ts, history, flagged_txn, device_info, is_coordinated,
            summary, case_id
        )

        # ── Step 23: What changed ──
        initial_action_names = {a.action for a in initial_actions}
        final_action_names = {a.action for a in final_actions}
        if initial_action_names == final_action_names:
            what_changed = "nothing"
        else:
            added = final_action_names - initial_action_names
            removed = initial_action_names - final_action_names
            parts = []
            if added:
                parts.append(f"Added: {', '.join(sorted(added))}")
            if removed:
                parts.append(f"Removed: {', '.join(sorted(removed))}")
            if assumed_response == 'denied':
                parts.append(f"Customer denial raised probability to {fraud_probability:.2f} and confirmed the block.")
            elif assumed_response == 'confirmed':
                parts.append("Customer confirmation cleared the alert.")
            what_changed = " ".join(parts)

        # ── Step 24: Assemble evidence list ──
        structured_evidence = self._build_evidence_list(
            flagged_txn, profile, history, testing, region_info,
            device_neighbors, device_cases, match_flags, similar_cases,
            domain_is_new, current_domain, historical_domains,
            trigger_type, risk_score, channel, id_15, id_23
        )

        # Merge LLM evidence with structured
        for ev in evidence_list:
            if ev not in structured_evidence:
                structured_evidence.append(ev)

        similar_prior_cases = [c['case_id'] for c in similar_cases[:5]]

        # ── Step 25: Write to graph ──
        case_record = {
            "case_id": case_id,
            "customer_id": customer_id,
            "card_id": card_id,
            "status": status,
            "verdict": verdict,
            "fraud_probability": fraud_probability,
            "pattern": pattern,
            "affected_txn_ids": affected_txn_ids,
            "exposure_usd": exposure,
            "connected_card_ids": connected_card_ids,
            "summary": summary or self._auto_summary(
                verdict, pattern, fraud_probability, exposure,
                affected_txn_ids, device_info, similar_prior_cases
            ),
        }
        # Write to SQLite (always) + TigerGraph via MCP (when TG_HOST is set)
        gq.write_investigation_case(case_record)
        tg_written = tg_mcp.write_case({**case_record, "card_id": card_id})
        written_to_graph = True  # SQLite always; TG MCP when configured

        latency = round(time.time() - t0, 1)
        tg_status = "TigerGraph MCP + SQLite" if tg_written else "SQLite"
        print(f"  → verdict={verdict} prob={fraud_probability:.2f} pattern={pattern} exposure=${exposure:.2f} [{latency}s] [{tg_status}]")

        return {
            "case_id": case_id,
            "case": {
                "status": status,
                "verdict": verdict,
                "fraud_probability": round(fraud_probability, 3),
                "pattern": pattern,
                "pattern_description": pattern_description,
                "affected_txn_ids": [str(t) for t in affected_txn_ids],
                "first_suspicious_txn_id": str(first_suspicious_txn_id) if first_suspicious_txn_id else "",
                "connected_card_ids": connected_card_ids,
                "connected_device_profiles": [device_info] if device_info and has_shared_device else [],
                "exposure_usd": round(exposure, 2),
                "evidence": structured_evidence,
                "similar_prior_cases": similar_prior_cases,
                "summary": case_record["summary"],
                "written_to_graph": written_to_graph,
                "graph_case_id": case_id,
            },
            "evidence_requests": evidence_requests,
            "next_best_actions": {
                "initial": _fmt_actions(initial_actions),
                "final": _fmt_actions(final_actions),
                "what_changed": what_changed,
            },
            "sar": sar,
            "stop_reason": stop_reason,
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "latency_s": latency,
        }

    def _build_evidence_context(self, flagged_txn, profile, history, window,
                                 testing, region_info, device_neighbors, device_cases,
                                 match_flags, similar_cases, historical_domains,
                                 current_domain, domain_is_new, trigger_type,
                                 trigger_text, risk_score, card_id) -> str:
        """Compact but complete evidence summary for the LLM."""
        lines = []

        lines.append(f"=== FLAGGED TRANSACTION ===")
        lines.append(f"ID: {flagged_txn.get('TransactionID')} | Amount: ${flagged_txn.get('TransactionAmt'):.2f}")
        lines.append(f"Time: {flagged_txn.get('ts')} | Channel: {flagged_txn.get('channel')}")
        lines.append(f"Product: {flagged_txn.get('ProductCD')} | addr1: {flagged_txn.get('addr1')} | addr2: {flagged_txn.get('addr2')}")
        lines.append(f"Email domain: {flagged_txn.get('P_emaildomain')} (new for customer: {domain_is_new})")
        lines.append(f"card4 (network): {flagged_txn.get('card4')} | card6 (type): {flagged_txn.get('card6')}")

        if flagged_txn.get('DeviceInfo'):
            lines.append(f"Device: {flagged_txn.get('DeviceInfo')}")
            lines.append(f"Device status (id_15): {flagged_txn.get('id_15')} | Proxy (id_23): {flagged_txn.get('id_23')}")
            lines.append(f"OS (id_30): {flagged_txn.get('id_30')} | Browser (id_31): {flagged_txn.get('id_31')}")

        if match_flags:
            lines.append(f"Match flags — mismatches: {match_flags.get('mismatch_count', 0)} ({list(match_flags.get('mismatches', {}).keys())})")

        lines.append(f"\n=== CUSTOMER PROFILE (before alert) ===")
        lines.append(f"Total transactions: {profile.get('n_txns', 0)}")
        lines.append(f"Avg amount: ${profile.get('avg_amount', 0):.2f} | Max: ${profile.get('max_amount') or 0:.2f}")
        lines.append(f"Products used: {[p['product'] for p in profile.get('product_distribution', [])]}")
        lines.append(f"Channels: {[c['channel'] for c in profile.get('channel_distribution', [])]}")
        lines.append(f"Distinct regions: {profile.get('n_distinct_regions', 0)}")

        if region_info:
            lines.append(f"\n=== REGION ANALYSIS (addr1={region_info.get('addr1')}) ===")
            lines.append(f"Times in this region: {region_info.get('txns_in_this_region', 0)}")
            lines.append(f"Is new region for customer: {region_info.get('is_new_region', False)}")
            lines.append(f"Familiar regions: {[r['addr1'] for r in region_info.get('familiar_regions', [])[:5]]}")

        lines.append(f"\n=== TRANSACTION WINDOW (±48h) ===")
        w_txns = window[:10] if window else []
        if w_txns:
            for t in w_txns:
                lines.append(f"  {t['ts']} | ${t.get('TransactionAmt',0):.2f} | {t['channel']} | {t['ProductCD']} | id_15={t.get('id_15','')} | {t.get('DeviceInfo','')[:40] if t.get('DeviceInfo') else 'no device'}")
        else:
            lines.append("  No other transactions in window")

        if testing.get('has_testing_pattern'):
            lines.append(f"\n=== CARD TESTING PATTERN DETECTED ===")
            lines.append(f"Testing cluster: {len(testing.get('testing_cluster',[]))} small transactions")
            for t in testing.get('testing_cluster', []):
                lines.append(f"  {t['ts']} | ${t.get('TransactionAmt',0):.2f} | {t['channel']}")

        if device_neighbors:
            lines.append(f"\n=== DEVICE SHARED WITH OTHER CARDS ===")
            lines.append(f"Other cards on same device: {len(device_neighbors)}")
            for dn in device_neighbors[:5]:
                lines.append(f"  customer={dn['customer_id']} card1={dn['card1']} id_15={dn.get('id_15','')} last={dn.get('last_seen','')[:10]}")

        if device_cases:
            lines.append(f"\n=== CLOSED CASES ON SAME DEVICE ===")
            for dc in device_cases[:3]:
                lines.append(f"  {dc['case_id']} | {dc['outcome']} | {dc['pattern']} | exposure=${dc['exposure_usd']}")

        if similar_cases:
            lines.append(f"\n=== SIMILAR PRIOR CASES (memory) ===")
            for sc in similar_cases[:5]:
                lines.append(f"  {sc['case_id']} | {sc['outcome']} | {sc['pattern']} | ${sc['exposure_usd']} | {sc['analyst_notes'][:120] if sc['analyst_notes'] else ''}")

        return "\n".join(lines)

    def _llm_analyze(self, evidence_context: str, case_id: str, customer_id: str,
                     card_id: str, flagged_txn_id: str, trigger_type: str,
                     risk_score: float) -> str:
        system = f"""You are a senior fraud analyst at a bank. You investigate card fraud cases.

{FRAUD_POLICY}
{KNOWN_PATTERNS}

IMPORTANT RULES FOR YOUR ANALYSIS:
- A high risk_score is a reason to look, NOT a verdict. Assess the full evidence.
- Half of all cases are legitimate. Do not over-flag.
- Be calibrated: fraud_probability should reflect evidence, not just risk_score.
- If the pattern is None or unclear, say so honestly — uncertainty is valid.
- For out_of_region_use: only apply if channel=in_person (ProductCD=W) in a new region.
- For card_not_present_new_device: channel must be online AND id_15 must be 'New'.
- For account_takeover: must show mixed-channel anomalies or match flag mismatches.

You MUST respond with ONLY a JSON object, no other text, with these exact fields:
{{
  "verdict": "fraud" | "legitimate" | "uncertain",
  "fraud_probability": 0.0-1.0,
  "pattern": "card_testing" | "card_not_present_fraud" | "card_not_present_new_device" | "out_of_region_use" | "account_takeover" | "undocumented" | "none",
  "pattern_description": "required if undocumented, else empty string",
  "affected_txn_ids": ["list of transaction IDs that are part of the fraud episode"],
  "first_suspicious_txn_id": "earliest suspicious transaction ID or empty",
  "connected_card_ids": ["other card IDs caught in same compromise, empty if none"],
  "evidence": [
    {{"claim": "one sentence claim", "source": "graph|document|customer|external", "ref": "query or source", "entity_ids": ["ids"]}}
  ],
  "summary": "2-6 sentence analyst summary"
}}"""

        user_msg = f"""Case {case_id} | Customer {customer_id} | Card {card_id}
Trigger: {trigger_type} | Flagged transaction: {flagged_txn_id} | Risk score: {risk_score if risk_score else 'N/A'}

EVIDENCE:
{evidence_context}

Analyze this case. Identify the fraud pattern (if any), assess probability, and list affected transactions.
Remember: many high-risk-score cases are legitimate. Look at the full behavioral context."""

        return self._llm(system, [{"role": "user", "content": user_msg}], max_tokens=2000)

    def _parse_analysis(self, analysis: str) -> dict:
        """Parse LLM JSON response, with fallback."""
        try:
            # Strip markdown code blocks if present
            text = analysis.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as e:
            print(f"  [WARN] Failed to parse LLM response: {e}")
            return {
                "verdict": "uncertain",
                "fraud_probability": 0.5,
                "pattern": "none",
                "pattern_description": "",
                "affected_txn_ids": [],
                "first_suspicious_txn_id": "",
                "connected_card_ids": [],
                "evidence": [{"claim": "LLM parse error — using structured signals only",
                               "source": "graph", "ref": "auto", "entity_ids": []}],
                "summary": "Investigation inconclusive due to analysis error. Manual review recommended.",
            }

    def _compute_exposure(self, txn_ids: list) -> float:
        if not txn_ids:
            return 0.0
        total = 0.0
        for tid in txn_ids:
            txn = gq.get_transaction(str(tid))
            if txn and txn.get('TransactionAmt'):
                total += abs(float(txn['TransactionAmt']))
        return round(total, 2)

    def _simulate_evidence_request(self, fraud_probability: float, trigger_type: str,
                                    pattern: str, exposure: float) -> tuple[str, dict]:
        """
        Simulate customer or analyst response.
        Customer-reported disputes → customer denied.
        High-probability fraud triggers → customer denied.
        Low-probability → customer confirmed.
        Medium → denied if pattern strong.
        """
        # Customer reports are genuine disputes
        if trigger_type == 'customer_report':
            response = 'denied'
            assumed = "Customer states they did not make this purchase and still have their card."
        elif fraud_probability >= 0.75:
            response = 'denied'
            assumed = "Customer confirms they did not make this transaction and have not shared their card details."
        elif fraud_probability <= 0.35:
            response = 'confirmed'
            assumed = "Customer confirms they made this transaction."
        elif pattern in ('card_testing', 'card_not_present_new_device'):
            response = 'denied'
            assumed = "Customer states they did not authorize these transactions."
        else:
            response = 'no_reply'
            assumed = "Customer did not respond within 24 hours."

        req = {
            "type": "customer_validation",
            "asked_after_step": 3,
            "assumed_response": assumed,
        }
        return response, req

    def _build_sar(self, file_report: bool, fraud_probability: float, exposure: float,
                   pattern: str, customer_id: str, card_id: str, affected_txn_ids: list,
                   connected_card_ids: list, txn_ts: str, history: list,
                   flagged_txn: dict, device_info: str, is_coordinated: bool,
                   summary: str, case_id: str) -> dict:
        if not file_report:
            reason = "No SAR required: "
            if fraud_probability < 0.60:
                reason += f"fraud probability {fraud_probability:.2f} does not meet threshold."
            elif exposure <= 1000:
                reason += f"exposure ${exposure:.2f} does not exceed $1,000 and no shared device."
            else:
                reason += "conditions for filing not met under current policy."
            return {
                "file": False,
                "reason": reason,
                "narrative": "",
                "subjects": [],
                "total_amount_usd": 0,
                "activity_dates": [],
            }

        # Determine date range of activity
        dates = sorted([t.get('ts', '')[:10] for t in history[:20] if t.get('ts')] + [txn_ts[:10]])
        activity_start = dates[0] if dates else txn_ts[:10]
        activity_end = txn_ts[:10]

        subjects = [customer_id, card_id] + connected_card_ids
        if device_info:
            subjects.append(f"device:{device_info[:50]}")

        # Build SAR narrative
        pattern_desc = {
            'card_testing': 'card testing — small authorizations followed by a larger purchase',
            'card_not_present_fraud': 'card-not-present fraud — unauthorized online purchases',
            'card_not_present_new_device': 'card-not-present fraud from a new device, consistent with stolen card number',
            'out_of_region_use': 'out-of-region card-present use, consistent with a cloned card',
            'account_takeover': 'account takeover — unauthorized access and mixed-channel activity',
            'undocumented': 'undocumented coordinated fraud pattern across multiple accounts',
        }.get(pattern, 'suspicious card activity')

        narrative = self._llm(
            "You are a compliance officer writing a Suspicious Activity Report (SAR) for a US bank. "
            "Write a SAR narrative following FinCEN guidance: who, what, when, where, how, why suspicious. "
            "6-12 sentences. Use formal regulatory language. No markdown.",
            [{
                "role": "user",
                "content": f"""Write a SAR narrative for this case:
Case: {case_id}
Customer: {customer_id} | Card: {card_id}
Pattern: {pattern_desc}
Affected transactions: {', '.join(str(t) for t in affected_txn_ids[:10])}
Exposure: ${exposure:.2f}
Connected cards: {connected_card_ids}
Date range: {activity_start} to {activity_end}
Device info: {device_info or 'N/A'}
Summary: {summary}

Write the SAR narrative now:"""
            }],
            max_tokens=600
        )

        return {
            "file": True,
            "reason": (f"R2/R6: fraud confirmed or strongly suspected with exposure ${exposure:.2f}. "
                       + ("Shared device links to additional compromised cards. " if connected_card_ids else "")
                       + "Policy threshold for regulatory filing met."),
            "narrative": narrative.strip(),
            "subjects": list(set(subjects)),
            "total_amount_usd": round(exposure, 2),
            "activity_dates": [activity_start, activity_end],
        }

    def _build_evidence_list(self, flagged_txn, profile, history, testing,
                              region_info, device_neighbors, device_cases,
                              match_flags, similar_cases, domain_is_new,
                              current_domain, historical_domains, trigger_type,
                              risk_score, channel, id_15, id_23) -> list[dict]:
        evidence = []
        tid = flagged_txn.get('TransactionID')

        # Trigger evidence
        if trigger_type == 'customer_report':
            evidence.append({
                "claim": "Customer explicitly disputed this transaction, stating they did not make it.",
                "source": "customer",
                "ref": "trigger:customer_report",
                "entity_ids": [tid],
            })
        elif trigger_type == 'analyst_request':
            evidence.append({
                "claim": "Analyst flagged this transaction as part of a broader device-sharing pattern investigation.",
                "source": "external",
                "ref": "trigger:analyst_request",
                "entity_ids": [tid],
            })

        if risk_score:
            evidence.append({
                "claim": f"Bank model risk score: {risk_score:.2f}. This is a signal for review, not a verdict.",
                "source": "graph",
                "ref": "field:risk_score",
                "entity_ids": [tid],
            })

        # Device evidence
        if id_15 == 'New':
            evidence.append({
                "claim": f"Device marked as 'New' for this account (id_15=New), consistent with a new or stolen device being used.",
                "source": "graph",
                "ref": "query:identity.id_15",
                "entity_ids": [tid],
            })
        if id_23 and 'hidden' in str(id_23).lower():
            evidence.append({
                "claim": f"Transaction originated from a hidden/anonymous proxy (id_23={id_23}), a known fraud indicator.",
                "source": "graph",
                "ref": "query:identity.id_23",
                "entity_ids": [tid],
            })

        # Match flag mismatches
        if match_flags.get('mismatch_count', 0) > 0:
            evidence.append({
                "claim": f"{match_flags['mismatch_count']} match flag mismatches detected: {list(match_flags.get('mismatches', {}).keys())}. These can indicate identity fraud.",
                "source": "graph",
                "ref": "query:match_flags",
                "entity_ids": [tid],
            })

        # Region evidence
        if region_info.get('is_new_region') and channel == 'in_person':
            evidence.append({
                "claim": f"Card-present transaction in billing region {region_info.get('addr1')} — customer has no transaction history in this region (0 prior transactions).",
                "source": "graph",
                "ref": "query:region_history",
                "entity_ids": [tid],
            })

        # Card testing
        if testing.get('has_testing_pattern'):
            cluster = testing.get('testing_cluster', [])
            evidence.append({
                "claim": f"Card testing pattern detected: {len(cluster)} small online authorizations (< $10 each) within 1 hour.",
                "source": "graph",
                "ref": "query:card_testing_signals",
                "entity_ids": [t.get('TransactionID') for t in cluster],
            })

        # Device sharing
        if device_neighbors:
            evidence.append({
                "claim": f"Device profile shared with {len(device_neighbors)} other cards from different customers — possible fraud ring.",
                "source": "graph",
                "ref": "query:device_neighbors",
                "entity_ids": [n.get('card1') for n in device_neighbors[:5]],
            })

        if device_cases:
            evidence.append({
                "claim": f"This device profile appears in {len(device_cases)} closed fraud cases: {[c['case_id'] for c in device_cases[:3]]}.",
                "source": "graph",
                "ref": "query:cases_by_device",
                "entity_ids": [c['case_id'] for c in device_cases[:3]],
            })

        # Domain anomaly
        if domain_is_new and current_domain:
            evidence.append({
                "claim": f"Purchaser email domain '{current_domain}' not seen in customer's prior transactions (historical: {historical_domains[:3]}).",
                "source": "graph",
                "ref": "query:email_domain_history",
                "entity_ids": [tid],
            })

        # Similar cases
        if similar_cases:
            fraud_cases = [c for c in similar_cases if c['outcome'] == 'confirmed_fraud']
            if fraud_cases:
                evidence.append({
                    "claim": f"Customer/card has {len(fraud_cases)} prior confirmed fraud cases: {[c['case_id'] for c in fraud_cases[:3]]}.",
                    "source": "graph",
                    "ref": "query:similar_closed_cases",
                    "entity_ids": [c['case_id'] for c in fraud_cases[:3]],
                })
            cleared_cases = [c for c in similar_cases if c['outcome'] == 'cleared']
            if cleared_cases and not fraud_cases:
                evidence.append({
                    "claim": f"Customer has {len(cleared_cases)} prior cleared alerts — previous investigations found no fraud.",
                    "source": "graph",
                    "ref": "query:similar_closed_cases",
                    "entity_ids": [c['case_id'] for c in cleared_cases[:3]],
                })

        # Profile mismatch
        avg_amt = profile.get('avg_amount', 0)
        flagged_amt = float(flagged_txn.get('TransactionAmt') or 0)
        if avg_amt > 0 and flagged_amt > avg_amt * 3:
            evidence.append({
                "claim": f"Transaction amount ${flagged_amt:.2f} is {flagged_amt/avg_amt:.1f}x the customer's average (${avg_amt:.2f}).",
                "source": "graph",
                "ref": "query:account_behavior_profile",
                "entity_ids": [tid],
            })

        return evidence

    def _auto_summary(self, verdict: str, pattern: str, fraud_probability: float,
                       exposure: float, affected_txn_ids: list, device_info: str,
                       similar_cases: list) -> str:
        if verdict == 'legitimate':
            return (f"Investigation closed as legitimate. Fraud probability assessed at {fraud_probability:.2f}. "
                    f"The flagged activity is consistent with the customer's historical behavior. No action required.")
        elif verdict == 'fraud':
            return (f"Investigation closed as confirmed fraud. Pattern: {pattern}. "
                    f"Fraud probability: {fraud_probability:.2f}. "
                    f"{len(affected_txn_ids)} transaction(s) affected, total exposure ${exposure:.2f}. "
                    f"{'Device profile links to additional compromised cards. ' if device_info else ''}"
                    f"{'Similar prior cases retrieved as investigation memory.' if similar_cases else ''}")
        else:
            return (f"Investigation inconclusive. Fraud probability {fraud_probability:.2f}. "
                    f"Estimated exposure ${exposure:.2f}. Additional review required.")

    def _error_case(self, case_id: str, customer_id: str, card_id: str, msg: str) -> dict:
        return {
            "case_id": case_id,
            "case": {
                "status": "escalated", "verdict": "uncertain",
                "fraud_probability": 0.5, "pattern": "none",
                "pattern_description": "", "affected_txn_ids": [],
                "first_suspicious_txn_id": "", "connected_card_ids": [],
                "connected_device_profiles": [], "exposure_usd": 0,
                "evidence": [{"claim": msg, "source": "graph",
                               "ref": "error", "entity_ids": []}],
                "similar_prior_cases": [], "summary": msg,
                "written_to_graph": False, "graph_case_id": "",
            },
            "evidence_requests": [],
            "next_best_actions": {
                "initial": [{"action": "ESCALATE_TO_ANALYST", "route": "auto",
                              "reason": f"Error during investigation: {msg}"}],
                "final": [{"action": "ESCALATE_TO_ANALYST", "route": "auto",
                           "reason": f"Error during investigation: {msg}"}],
                "what_changed": "nothing",
            },
            "sar": {"file": False, "reason": "Investigation error", "narrative": "",
                    "subjects": [], "total_amount_usd": 0, "activity_dates": []},
            "stop_reason": f"Investigation error: {msg}",
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "latency_s": 0,
        }
