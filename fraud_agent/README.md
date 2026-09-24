# TigerGraph Fraud Investigation Agent
### Hacker House Goa 2026 · IEEE-CIS Fraud Detection

An agentic fraud investigation system that investigates card fraud cases, maintains case memory in TigerGraph, and recommends next best actions under a real bank fraud policy.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Next.js Analyst Dashboard (localhost:3000)                  │
│  • Case list with verdicts, patterns, exposure               │
│  • Case detail: evidence, actions, SAR, graph connections    │
│  • Live investigation trigger                                │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────────┐
│  FastAPI Backend (localhost:8000)                            │
│  • /api/cases  /api/cases/{id}  /api/investigate             │
│  • /api/stats  /api/customer/{id}/transactions               │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  LangGraph-style Investigation Agent (agent/investigator.py) │
│                                                              │
│  trigger → gather_evidence → llm_analyze →                  │
│  assess_uncertainty → request_evidence →                     │
│  final_actions → write_to_graph → generate_output           │
│                                                              │
│  Tools (graph_queries.py):                                   │
│  • get_transaction(id)                                       │
│  • get_card_history(customer, card1, before_ts)              │
│  • get_transaction_window(card1, ts, hours)                  │
│  • get_card_testing_signals(card1, ts)                       │
│  • get_region_history(addr1, customer)                       │
│  • get_device_neighbors(device_info)                         │
│  • get_cards_sharing_device(device_info)                     │
│  • get_similar_closed_cases(customer, pattern)               │
│  • get_match_flag_anomalies(txn_id)                          │
│  • get_account_behavior_profile(customer)                    │
│  • write_investigation_case(case)                            │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  Data Layer (data/fraud.db — SQLite, mirrors TigerGraph)     │
│  590,742 transactions · 144,432 identity records             │
│  5,565 closed cases · 20 exam cases                         │
└─────────────────────────────────────────────────────────────┘
```

**TigerGraph Savanna** schema in `schema/tigergraph_schema.gsql`. The local SQLite layer mirrors the same graph structure and query API, letting the agent run identically against either backend. Connect TigerGraph MCP to swap in the real graph.

---

## Quick Start

```bash
# 1. Install dependencies
pip3 install anthropic fastapi uvicorn

# 2. Set API key
export ANTHROPIC_API_KEY=sk-ant-...

# 3. Build database (one-time, ~3 minutes)
cd fraud_agent
python3 data/build_db.py

# 4. Run all 20 investigations
python3 run_agent.py

# 5. Start API server
uvicorn api:app --reload --port 8000

# 6. Start UI (new terminal)
cd ui && npm install && npm run dev

# Or run everything at once:
bash setup_and_run.sh
```

---

## What the Agent Does

For each of the 20 exam cases, the agent:

1. **Fetches the flagged transaction** and joins it with the identity record
2. **Builds a behavioral profile** — the customer's historical amounts, products, channels, regions
3. **Examines a ±48h transaction window** around the flagged event
4. **Checks for card testing** (3+ sub-$10 online auths in 1hr, then a larger purchase)
5. **Analyzes the billing region** — is this a new region for the cardholder?
6. **Queries device neighbors** — what other cards shared this device profile?
7. **Checks match flags** — M1–M9 mismatches signal identity fraud
8. **Retrieves similar closed cases** from the 5,565 historical cases (case memory)
9. **Asks Claude** to synthesize all evidence, identify the fraud pattern, and assess probability
10. **Applies the fraud policy** (R1–R10) to generate initial and final actions
11. **Simulates evidence requests** (customer validation) and updates recommendations
12. **Generates a SAR** when policy requires regulatory filing
13. **Writes the case to the graph** so future investigations can find it

---

## Output Format

Each case produces `cases/HHG-NNN.json` with:
- **case**: status, verdict, fraud_probability, pattern, affected transactions, evidence list, summary
- **sar**: whether to file, full narrative (who/what/when/where/how/why)
- **next_best_actions**: initial (before evidence), final (after), what changed
- **evidence_requests**: what was asked, assumed response
- **stop_reason**: why investigation ended

---

## Policy Implementation

All 10 policy rules (R1–R10) are implemented in `agent/policy.py`:
- R1: Verify before blocking on weak signals (prob < 0.70)
- R2: Customer denial → block + case + SAR if exposed
- R3: Customer confirmation → close as legitimate
- R4: No reply → monitor + decline
- R5: Card testing sequence → step-up or block
- R6: Shared device/region → monitor connected cards
- R7: Disputed recurring pattern → don't block
- R8: Uncertain + exposed → escalate
- R9: Undocumented coordinated → SAR + escalate
- R10: BLOCK_ALL_CARDS only with 2+ confirmed compromised

---

## Fraud Patterns Detected

- `card_testing` — small auth sequence before purchase
- `card_not_present_fraud` — online burst inconsistent with history
- `card_not_present_new_device` — online + id_15=New + optional proxy
- `out_of_region_use` — in_person in unfamiliar billing region
- `account_takeover` — mixed-channel anomalies + match flag mismatches
- `undocumented` — coordinated activity fitting none of the above

---

## Files

```
fraud_agent/
├── data/
│   ├── build_db.py         # Load CSVs into SQLite
│   └── fraud.db            # Local graph database (590k transactions)
├── agent/
│   ├── graph_queries.py    # All graph/DB access (TigerGraph-compatible API)
│   ├── investigator.py     # Core investigation agent (Claude claude-sonnet-4-6)
│   └── policy.py           # Fraud policy engine (R1-R10)
├── cases/                  # Output: 20 JSON answer files
├── schema/
│   └── tigergraph_schema.gsql  # TigerGraph schema + GSQL queries
├── ui/                     # Next.js analyst dashboard
├── api.py                  # FastAPI backend
├── run_agent.py            # CLI to run investigations
└── setup_and_run.sh        # One-command launcher
```
