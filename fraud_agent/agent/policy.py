"""
Fraud policy engine — implements the bank's rules exactly as written in README.
Every recommendation is traceable to a rule number.
"""

from dataclasses import dataclass, field
from typing import Literal

ActionName = Literal[
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS", "GENERATE_REPORT",
    "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"
]

RouteType = Literal["auto", "L1", "L2"]

AUTO_ACTIONS = {
    "ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH",
    "GENERATE_REPORT", "CREATE_CASE", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"
}


@dataclass
class Action:
    action: ActionName
    route: RouteType
    reason: str


def get_route(action: ActionName, exposure: float = 0) -> RouteType:
    if action in AUTO_ACTIONS:
        return "auto"
    if action == "DECLINE_TRANSACTION":
        return "L1"
    if action == "BLOCK_CARD":
        return "L1" if exposure <= 2500 else "L2"
    if action in ("BLOCK_ALL_CARDS", "FILE_REPORT"):
        return "L2"
    return "auto"


def should_open_case(fraud_probability: float, customer_disputed: bool,
                     has_evidence_request: bool) -> bool:
    """R3a: open a case when fraud prob >= 0.30, dispute exists, or evidence requested."""
    return fraud_probability >= 0.30 or customer_disputed or has_evidence_request


def should_file_report(fraud_probability: float, exposure: float,
                       has_shared_device: bool, has_connected_fraud: bool,
                       is_coordinated: bool) -> bool:
    """
    FILE_REPORT when fraud is confirmed or strongly suspected AND:
    - exposure > $1,000, OR
    - shared device/region with another fraud, OR
    - coordinated/undocumented pattern (R9)
    """
    if fraud_probability < 0.60:
        return False
    return exposure > 1000 or has_shared_device or has_connected_fraud or is_coordinated


def build_initial_actions(
    fraud_probability: float,
    exposure: float,
    trigger_type: str,
    pattern: str,
    has_card_testing: bool,
    has_shared_device: bool,
    customer_disputed: bool,
) -> list[Action]:
    """
    Initial recommendation — before any evidence is requested.
    Follows R1-R9 strictly.
    """
    actions = []

    # R5: Card testing — most aggressive early action
    if has_card_testing:
        actions.append(Action("DECLINE_TRANSACTION", "L1",
                               "R5: card-testing sequence observed — decline active authorization"))
        if exposure > 100:
            actions.append(Action("VERIFY_WITH_CUSTOMER", "auto",
                                   "R5 + R1: confirm before blocking"))
        else:
            actions.append(Action("STEP_UP_AUTH", "auto",
                                   "R5: testing pattern detected, step-up before further activity"))
        return actions

    # Customer dispute (R2 pre-verification)
    if customer_disputed:
        if fraud_probability >= 0.70:
            actions.append(Action("BLOCK_CARD", get_route("BLOCK_CARD", exposure),
                                   "R2: customer disputed, fraud probability high"))
            actions.append(Action("CREATE_CASE", "auto", "R2: customer dispute warrants a case"))
            if should_file_report(fraud_probability, exposure, has_shared_device, False, False):
                actions.append(Action("FILE_REPORT", "L2",
                                       "R2: exposure > $1,000 or shared device"))
        else:
            # Still need to verify
            actions.append(Action("VERIFY_WITH_CUSTOMER", "auto",
                                   "R1: dispute received but probability below 0.70, confirm before blocking"))
            actions.append(Action("CREATE_CASE", "auto",
                                   "R3a: customer dispute warrants a case"))
            actions.append(Action("MONITOR_CARD", "auto",
                                   "R4: monitoring while waiting for confirmation"))
        return actions

    # Risk-score or analyst trigger — single signal
    if fraud_probability < 0.70:
        # R1: weak signal, verify first
        actions.append(Action("VERIFY_WITH_CUSTOMER", "auto",
                               f"R1: fraud probability {fraud_probability:.2f} is below 0.70 on single signal — verify before any block"))
        actions.append(Action("MONITOR_CARD", "auto",
                               "R1: card monitored while awaiting response"))
        if fraud_probability >= 0.30:
            actions.append(Action("CREATE_CASE", "auto",
                                   "R3a: probability >= 0.30 warrants opening a case"))
    elif fraud_probability < 0.85:
        # Medium-high: step-up + monitor
        actions.append(Action("STEP_UP_AUTH", "auto",
                               f"R1: probability {fraud_probability:.2f} — step-up authentication before further activity"))
        actions.append(Action("MONITOR_CARD", "auto", "Monitor card activity"))
        actions.append(Action("CREATE_CASE", "auto", "R3a: probability >= 0.30"))
        if has_shared_device:
            actions.append(Action("MONITOR_CONNECTED_CARDS", "auto",
                                   "R6: shared device profile — monitor connected cards"))
    else:
        # High: block
        actions.append(Action("BLOCK_CARD", get_route("BLOCK_CARD", exposure),
                               f"High fraud probability {fraud_probability:.2f}"))
        actions.append(Action("CREATE_CASE", "auto", "R3a"))
        if has_shared_device:
            actions.append(Action("MONITOR_CONNECTED_CARDS", "auto", "R6"))
        if should_file_report(fraud_probability, exposure, has_shared_device, False, False):
            actions.append(Action("FILE_REPORT", "L2",
                                   f"R2/R6: high confidence fraud, exposure ${exposure:.2f}"))

    # R8: escalate when uncertain and exposed
    if 0.40 <= fraud_probability <= 0.60 and exposure > 500:
        actions.append(Action("ESCALATE_TO_ANALYST", "auto",
                               "R8: uncertain verdict with exposure > $500 — escalate"))

    return actions


def build_final_actions(
    fraud_probability: float,
    exposure: float,
    pattern: str,
    customer_response: str,  # 'denied', 'confirmed', 'no_reply', 'pending'
    has_shared_device: bool,
    has_connected_fraud: bool,
    is_coordinated: bool,
    connected_card_ids: list[str],
) -> list[Action]:
    """
    Final recommendation after assumed evidence response.
    """
    actions = []

    if customer_response == 'confirmed':
        # R3: customer confirmed — close as legitimate
        actions.append(Action("CLOSE_NO_FRAUD", "auto",
                               "R3: customer confirmed the transaction"))
        return actions

    if customer_response == 'no_reply':
        # R4: no reply in 24h
        actions.append(Action("MONITOR_CARD", "auto",
                               "R4: no reply in 24 hours — monitoring"))
        actions.append(Action("DECLINE_TRANSACTION", "L1",
                               "R4: no reply — decline pending authorizations"))
        if exposure > 500:
            actions.append(Action("ESCALATE_TO_ANALYST", "auto",
                                   "R4: no reply with exposure > $500"))
        return actions

    if customer_response == 'denied':
        # R2: customer denied
        actions.append(Action("BLOCK_CARD", get_route("BLOCK_CARD", exposure),
                               f"R2: customer denied transaction, block and reissue card"))
        actions.append(Action("CREATE_CASE", "auto",
                               "R2: customer denial — open fraud case"))
        file_needed = should_file_report(fraud_probability, exposure,
                                          has_shared_device, has_connected_fraud, is_coordinated)
        if file_needed:
            actions.append(Action("FILE_REPORT", "L2",
                                   "R2: " + (
                                       f"exposure ${exposure:.2f} > $1,000" if exposure > 1000
                                       else "shared device links to another compromised card"
                                   )))
        if has_shared_device or connected_card_ids:
            actions.append(Action("MONITOR_CONNECTED_CARDS", "auto",
                                   "R6: shared device/region — monitor connected cards"))
        return actions

    # No customer response yet (risk_score/analyst trigger)
    if fraud_probability >= 0.85:
        actions.append(Action("BLOCK_CARD", get_route("BLOCK_CARD", exposure),
                               f"R2/R5: fraud probability {fraud_probability:.2f} — block card"))
        actions.append(Action("CREATE_CASE", "auto", "R3a"))
        if should_file_report(fraud_probability, exposure, has_shared_device,
                               has_connected_fraud, is_coordinated):
            actions.append(Action("FILE_REPORT", "L2",
                                   f"Confirmed fraud with exposure ${exposure:.2f}"))
        if has_shared_device or connected_card_ids:
            actions.append(Action("MONITOR_CONNECTED_CARDS", "auto", "R6"))
    elif fraud_probability <= 0.15:
        actions.append(Action("CLOSE_NO_FRAUD", "auto",
                               "Fraud probability ≤ 0.15 with supporting evidence"))
        actions.append(Action("GENERATE_REPORT", "auto",
                               "Document investigation findings"))
    else:
        actions.append(Action("MONITOR_CARD", "auto",
                               f"Inconclusive — monitoring card"))
        actions.append(Action("ESCALATE_TO_ANALYST", "auto",
                               "R8: uncertain verdict, escalate for human review"))
        if fraud_probability >= 0.30:
            actions.append(Action("CREATE_CASE", "auto", "R3a"))

    # R9: undocumented coordinated pattern
    if is_coordinated and pattern == 'undocumented':
        if not any(a.action == "FILE_REPORT" for a in actions):
            actions.append(Action("FILE_REPORT", "L2",
                                   "R9: undocumented coordinated pattern — regulatory filing required"))
        if not any(a.action == "ESCALATE_TO_ANALYST" for a in actions):
            actions.append(Action("ESCALATE_TO_ANALYST", "auto",
                                   "R9: undocumented pattern — escalate"))

    return actions


def should_stop(fraud_probability: float, n_evidence_pieces: int,
                has_verification_response: bool) -> tuple[bool, str]:
    """R6 stopping rule."""
    if has_verification_response:
        return True, "Verification response received — verdict settled"
    if fraud_probability >= 0.85 and n_evidence_pieces >= 2:
        return True, "Fraud probability >= 0.85 with 2+ independent evidence pieces"
    if fraud_probability <= 0.15 and n_evidence_pieces >= 2:
        return True, "Fraud probability <= 0.15 with 2+ independent evidence pieces"
    return False, ""
