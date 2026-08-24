"""
Deterministic, non-AI validation helpers for AI-extracted financial documents.

These run *after* the LLM extraction step and add a trust/safety layer without
spending any extra tokens:

  - validate_financials(): recalculates subtotal/tax/grand total from the
    extracted line items and tax entries, then cross-checks that against the
    AI-reported grand total within a tolerance band. Flags a HIGH risk_level
    if they disagree by more than the tolerance.

  - intelligent_supplier_match(): exact-match first, then a fuzzy
    difflib.SequenceMatcher pass against existing Suppliers. Deliberately
    returns no match (rather than guessing) when multiple suppliers are
    ambiguously close, so a human reviews it instead of an auto-pick being
    silently wrong.

Both are pure Python — no LLM calls, no network — so they're fast and free
to run on every extraction.
"""

import difflib
import re

import frappe


def _safe_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def validate_financials(items, tax_amount=0, grand_total=0, tolerance_percent=0.02):
    """
    Recomputes subtotal/tax/grand total from extracted items and compares
    against the AI-reported grand_total.

    items: list of dicts with qty/rate and/or amount keys (same shape used
           by create_purchase_invoice's extracted items list).
    tax_amount: AI-reported total tax (single number, matches this app's
                extraction schema — unlike Zikpro's per-line tax array).
    grand_total: AI-reported grand total to validate against.
    tolerance_percent: allowed relative difference before flagging HIGH risk
                        (default 2%, matches typical rounding/tax slack).

    Returns a dict safe to send straight to the frontend for display.
    """
    calculated_subtotal = 0.0
    for item in (items or []):
        qty = _safe_float(item.get("qty"))
        rate = _safe_float(item.get("rate"))
        amount = _safe_float(item.get("amount"))
        if qty > 0 and rate > 0:
            calculated_subtotal += qty * rate
        else:
            calculated_subtotal += amount

    calculated_tax = _safe_float(tax_amount)
    calculated_grand_total = calculated_subtotal + calculated_tax
    detected_total = _safe_float(grand_total)

    mismatch_amount = abs(calculated_grand_total - detected_total)
    allowed_difference = (detected_total * tolerance_percent) if detected_total > 0 else 1.0

    is_valid = mismatch_amount <= allowed_difference
    risk_level = "LOW" if is_valid else "HIGH"
    confidence_adjustment = 5 if is_valid else -20

    return {
        "is_valid": is_valid,
        "calculated_subtotal": round(calculated_subtotal, 2),
        "calculated_tax": round(calculated_tax, 2),
        "calculated_grand_total": round(calculated_grand_total, 2),
        "detected_grand_total": round(detected_total, 2),
        "mismatch_amount": round(mismatch_amount, 2),
        "risk_level": risk_level,
        "confidence_adjustment": confidence_adjustment,
    }


_LEGAL_SUFFIX_TOKENS = {
    "co", "company", "corp", "corporation", "inc", "incorporated",
    "limited", "ltd", "llc", "lp", "plc", "private", "pvt",
}


def _normalise_business_name(value):
    """Return meaningful business-name tokens, ignoring legal suffixes."""
    tokens = re.findall(r"[a-z0-9]+", (value or "").casefold())
    return [token for token in tokens if token not in _LEGAL_SUFFIX_TOKENS]


def intelligent_entity_match(doctype, name_field, detected_name):
    """
    Match an extracted name to an existing record without silently guessing.

    Matching is exact first, then compares legal-name-normalised forms. A
    short ERPNext name may be accepted when its meaningful token(s) occur in
    the detected legal invoice name, but only where exactly one record
    qualifies. General fuzzy matching remains the final, conservative path.
    """
    if not detected_name:
        return {"match": None, "confidence": 0, "multiple_matches": False}

    needle = detected_name.strip().casefold()
    needle_tokens = _normalise_business_name(detected_name)
    if not needle or not needle_tokens:
        return {"match": None, "confidence": 0, "multiple_matches": False}

    records = frappe.get_all(doctype, fields=["name", name_field])

    # 1. Exact match (case-insensitive).
    for record in records:
        value = record.get(name_field)
        if value and value.strip().casefold() == needle:
            return {"match": record.name, "confidence": 100, "multiple_matches": False}

    # 2. Exact match after harmless legal-suffix/punctuation normalisation.
    normalised_matches = []
    for record in records:
        value = record.get(name_field)
        if value and _normalise_business_name(value) == needle_tokens:
            normalised_matches.append(record.name)
    if len(normalised_matches) == 1:
        return {"match": normalised_matches[0], "confidence": 98, "multiple_matches": False}
    if len(normalised_matches) > 1:
        return {"match": None, "confidence": 98, "multiple_matches": True}

    # 3. Legal invoice names often add product words to a short supplier name
    # (for example, "KEFALOS CHEESE PRODUCTS PVT (LTD)" -> "Kefalos").
    # Only accept a unique, meaningful token-sequence containment match.
    contained_matches = []
    for record in records:
        value = record.get(name_field)
        candidate_tokens = _normalise_business_name(value)
        if (
            candidate_tokens
            and all(len(token) >= 4 for token in candidate_tokens)
            and set(candidate_tokens).issubset(set(needle_tokens))
        ):
            contained_matches.append(record.name)
    if len(contained_matches) == 1:
        return {"match": contained_matches[0], "confidence": 95, "multiple_matches": False}
    if len(contained_matches) > 1:
        return {"match": None, "confidence": 95, "multiple_matches": True}

    # 4. Fuzzy similarity is deliberately the final fallback.
    scores = []
    for record in records:
        value = record.get(name_field)
        if not value:
            continue
        score = difflib.SequenceMatcher(None, needle, value.strip().casefold()).ratio()
        scores.append((score, record.name))

    if not scores:
        return {"match": None, "confidence": 0, "multiple_matches": False}

    scores.sort(reverse=True, key=lambda item: item[0])
    best_score, best_match = scores[0]
    confidence = int(best_score * 100)

    close_matches = [score for score in scores if score[0] > 0.75]
    if len(close_matches) > 1:
        return {"match": None, "confidence": confidence, "multiple_matches": True}

    if confidence >= 80:
        return {"match": best_match, "confidence": confidence, "multiple_matches": False}

    return {"match": None, "confidence": confidence, "multiple_matches": False}

def intelligent_supplier_match(detected_name):
    """
    Matches an AI-extracted supplier name string against existing Supplier
    records. Thin wrapper over intelligent_entity_match(), kept as its own
    function (and its own "supplier" response key) for backward
    compatibility with the existing Purchase Invoice flow.

    Returns: {"supplier": name_or_None, "confidence": 0-100, "multiple_matches": bool}
    """
    result = intelligent_entity_match("Supplier", "supplier_name", detected_name)
    return {
        "supplier": result["match"],
        "confidence": result["confidence"],
        "multiple_matches": result["multiple_matches"],
    }
