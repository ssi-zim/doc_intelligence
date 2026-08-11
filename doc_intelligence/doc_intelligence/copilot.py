"""
Ask ERPNext in plain English — safely.

Pipeline: question -> LLM proposes a structured JSON query plan (schema-aware,
using real DocType metadata) -> plan is validated against an explicit allow-list
and the current user's own permissions -> ERPNext executes it via
frappe.get_list()/frappe.get_doc() as the current user. No raw SQL, no generated
Python, no ignore_permissions. Record creation is a two-step propose -> approve
flow; nothing is written to the database until the user explicitly approves.
"""

import json
import re

import frappe
from frappe.utils import cint

from doc_intelligence.doc_intelligence.llm_engine import llm_call

# ---------------------------------------------------------------------------
# Allow-lists. These are the only DocTypes the copilot will ever touch, and
# only if they're actually installed on the site. Doc Intelligence's own
# DocTypes (Settings, Provider Log, ...) are deliberately excluded so a
# question can never be used to read back API keys or provider logs.
# ---------------------------------------------------------------------------

READ_DOCTYPES = [
    "Sales Invoice", "Purchase Invoice", "Sales Order", "Purchase Order",
    "Quotation", "Delivery Note", "Purchase Receipt", "Payment Entry",
    "Material Request", "Stock Entry", "Journal Entry",
    "Item", "Customer", "Supplier", "Employee", "Lead", "Opportunity",
    "Task", "ToDo", "Contact", "Address",
]

# Deliberately excludes anything transactional/financial — drafts created
# here can only ever be simple master records with no GL/stock side effects.
CREATE_DOCTYPES = ["Customer", "Supplier", "Item", "Task", "ToDo", "Contact", "Lead"]

EXCLUDED_FIELDTYPES = {
    "Attach", "Attach Image", "Password", "Text Editor", "Code", "HTML",
    "HTML Editor", "Signature", "Geolocation", "Table", "Table MultiSelect",
    "Markdown Editor", "Read Only",
}

STANDARD_FIELDS = {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}

ALLOWED_OPERATORS = {
    "=", "!=", "<", ">", "<=", ">=", "like", "not like",
    "in", "not in", "between", "is",
}

MAX_LIMIT = 100
DEFAULT_LIMIT = 20
DEFAULT_GET_LIMIT = 5


class CopilotError(Exception):
    """Raised for anything the user should see as a plain-English failure
    (bad plan, permission denied, unknown doctype, ...)."""


# ---------------------------------------------------------------------------
# Schema context
# ---------------------------------------------------------------------------

def _installed_doctypes(candidates):
    return [d for d in candidates if frappe.db.exists("DocType", d)]


def _field_schema(doctype):
    meta = frappe.get_meta(doctype)
    fields = {}
    for df in meta.fields:
        if df.fieldtype in EXCLUDED_FIELDTYPES:
            continue
        entry = {"label": df.label or df.fieldname, "type": df.fieldtype}
        if df.fieldtype in ("Link", "Select") and df.options:
            entry["options"] = df.options if df.fieldtype == "Link" else df.options.split("\n")[:15]
        fields[df.fieldname] = entry
    return fields


def get_schema_context(user=None):
    """Real DocType metadata for every allow-listed DocType installed on this
    site AND readable by the given user — this is what actually gets sent to
    the LLM, never raw business records."""
    user = user or frappe.session.user
    schema = {}
    for doctype in _installed_doctypes(READ_DOCTYPES):
        if not frappe.has_permission(doctype, ptype="read", user=user):
            continue
        schema[doctype] = _field_schema(doctype)
    return schema


def get_create_schema_context(user=None):
    user = user or frappe.session.user
    schema = {}
    for doctype in _installed_doctypes(CREATE_DOCTYPES):
        if not frappe.has_permission(doctype, ptype="create", user=user):
            continue
        meta = frappe.get_meta(doctype)
        mandatory = [df.fieldname for df in meta.fields if df.reqd and df.fieldtype not in EXCLUDED_FIELDTYPES]
        schema[doctype] = {"fields": _field_schema(doctype), "mandatory": mandatory}
    return schema


DEMO_PROMPTS = [
    "Show unpaid invoices older than 30 days",
    "Show unpaid invoices",
    "Show low-stock items",
    "Show this month's sales invoices",
    "How many sales orders were created this week?",
    "Show customers with overdue payments",
    "Show purchase orders waiting for approval",
    "Show today's deliveries",
    "Find customer ABC",
]


# ---------------------------------------------------------------------------
# Planning (LLM)
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = """You are a query planner for an ERP system. Given a user's plain-English \
question and a JSON schema of the DocTypes/fields actually available, output ONE JSON object \
describing how to answer it — never the answer itself, and never SQL or code.

Output schema (all keys required unless noted):
{
  "action": "list" | "count" | "get" | "create_draft" | "unsupported",
  "doctype": "<one of the schema doctypes, or the create-doctype for create_draft>",
  "filters": [["fieldname", "operator", value], ...],
  "fields": ["fieldname", ...],           // omit or [] to use sensible defaults
  "order_by": "fieldname asc|desc",       // omit if not relevant
  "limit": <int, omit for default>,
  "values": {"fieldname": value, ...},    // ONLY for create_draft
  "note": "<one short sentence of what you're about to do, shown to the user>"
}

Rules:
- Only use doctypes and fieldnames that literally appear in the provided schema. Never invent one.
- operator must be one of: =, !=, <, >, <=, >=, like, not like, in, not in, between, is.
- For "like" values, include the % wildcards yourself, e.g. "%ABC%".
- Dates: use ISO format (YYYY-MM-DD). Resolve relative dates ("this month", "last 30 days", \
"this week") against the "today" value given to you.
- "count" means the user wants a number, not a list of rows.
- "get" means the user wants a single record or a very small, specific set (e.g. "find customer ABC").
- Use "create_draft" only when the user is explicitly asking to create/add a new record, and only \
for doctypes present in the create-schema. Fill "values" only with fields present in that doctype's \
schema; always include every field listed as mandatory for it if you can infer a reasonable value, \
otherwise leave it out and let the user fill it in.
- If the question can't be answered with the given schema (wrong domain, needs a delete/submit/ \
payment/refund/raw SQL, or asks about a doctype not in the schema), set action to "unsupported" and \
explain briefly in "note".
- Respond with the JSON object only. No markdown, no commentary."""


def _build_planner_prompt(question, schema, create_schema, today):
    return json.dumps({
        "today": today,
        "question": question,
        "readable_doctypes_and_fields": schema,
        "creatable_doctypes_and_fields": create_schema,
    }, default=str)


def _strip_fences(text):
    return re.sub(r"```json\s*|\s*```", "", text or "").strip()


def build_query_plan(question, user=None):
    user = user or frappe.session.user
    schema = get_schema_context(user)
    create_schema = get_create_schema_context(user)
    if not schema:
        raise CopilotError(
            "No supported ERPNext DocTypes are readable for your account yet. "
            "Ask your administrator to grant read access to the relevant doctypes."
        )
    settings = frappe.get_single("Doc Intelligence Settings")
    prompt = _build_planner_prompt(question, schema, create_schema, frappe.utils.nowdate())
    result = llm_call(
        prompt, _PLANNER_SYSTEM,
        max_tokens=settings.max_tokens_per_request or 2000,
        tenant_name=user, json_mode=True,
    )
    try:
        plan = json.loads(_strip_fences(result["text"]))
    except Exception:
        raise CopilotError("The AI didn't return a valid query plan. Please rephrase your question.")
    if not isinstance(plan, dict):
        raise CopilotError("The AI didn't return a valid query plan. Please rephrase your question.")
    plan["_meta"] = {"provider": result.get("provider"), "model": result.get("model")}
    return plan


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _is_safe_scalar(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return len(value) <= 200 if isinstance(value, str) else True
    return False


def _is_safe_value(value):
    if isinstance(value, list):
        return len(value) <= 50 and all(_is_safe_scalar(v) for v in value)
    return _is_safe_scalar(value)


def _valid_columns(doctype):
    meta = frappe.get_meta(doctype)
    cols = {df.fieldname for df in meta.fields if df.fieldtype not in ("Table", "Table MultiSelect")}
    return cols | STANDARD_FIELDS


def _validate_filters(doctype, filters):
    if filters is None:
        return []
    if not isinstance(filters, list):
        raise CopilotError("The AI proposed an invalid filter list.")
    valid_cols = _valid_columns(doctype)
    clean = []
    for f in filters:
        if not (isinstance(f, list) and len(f) == 3):
            raise CopilotError("The AI proposed a malformed filter.")
        fieldname, operator, value = f
        if fieldname not in valid_cols:
            raise CopilotError(f"'{fieldname}' isn't a field on {doctype}.")
        if operator not in ALLOWED_OPERATORS:
            raise CopilotError(f"'{operator}' isn't an allowed filter operator.")
        if not _is_safe_value(value):
            raise CopilotError("The AI proposed an unsafe filter value.")
        clean.append([fieldname, operator, value])
    return clean


def _validate_fields(doctype, fields, default_limit_fields=True):
    valid_cols = _valid_columns(doctype)
    if not fields:
        if default_limit_fields:
            meta = frappe.get_meta(doctype)
            preferred = [df.fieldname for df in meta.fields if df.in_list_view and df.fieldtype not in EXCLUDED_FIELDTYPES]
            return (["name"] + preferred)[:10] or ["name"]
        return ["name"]
    if not isinstance(fields, list):
        raise CopilotError("The AI proposed an invalid field list.")
    clean = [f for f in fields if isinstance(f, str) and f in valid_cols]
    if not clean:
        raise CopilotError("None of the AI's requested fields exist on this doctype.")
    return clean[:25]


def _validate_order_by(doctype, order_by):
    if not order_by or not isinstance(order_by, str):
        return "creation desc"
    parts = order_by.strip().split()
    if len(parts) != 2 or parts[1].lower() not in ("asc", "desc"):
        return "creation desc"
    fieldname = parts[0]
    if fieldname not in _valid_columns(doctype):
        return "creation desc"
    return f"{fieldname} {parts[1].lower()}"


def validate_plan(plan, user=None):
    user = user or frappe.session.user
    action = plan.get("action")
    if action not in ("list", "count", "get", "create_draft", "unsupported"):
        raise CopilotError("The AI proposed an unrecognised action.")

    if action == "unsupported":
        return {"action": "unsupported", "note": plan.get("note") or "I can't answer that with the data available."}

    doctype = plan.get("doctype")
    if not isinstance(doctype, str) or not doctype:
        raise CopilotError("The AI didn't specify a doctype.")

    if action == "create_draft":
        if doctype not in CREATE_DOCTYPES or not frappe.db.exists("DocType", doctype):
            raise CopilotError(f"Creating '{doctype}' records isn't supported here.")
        if not frappe.has_permission(doctype, ptype="create", user=user):
            raise CopilotError(f"You don't have permission to create {doctype} records.")
        values = plan.get("values") or {}
        if not isinstance(values, dict):
            raise CopilotError("The AI proposed invalid field values.")
        valid_cols = _valid_columns(doctype) - {"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"}
        clean_values = {}
        for k, v in values.items():
            if k in valid_cols and _is_safe_value(v):
                clean_values[k] = v
        return {"action": "create_draft", "doctype": doctype, "values": clean_values,
                "note": plan.get("note") or f"Create a new {doctype}"}

    if doctype not in READ_DOCTYPES or not frappe.db.exists("DocType", doctype):
        raise CopilotError(f"'{doctype}' isn't a supported doctype for questions.")
    if not frappe.has_permission(doctype, ptype="read", user=user):
        raise CopilotError(f"You don't have permission to read {doctype} records.")

    filters = _validate_filters(doctype, plan.get("filters"))
    limit = cint(plan.get("limit") or (DEFAULT_GET_LIMIT if action == "get" else DEFAULT_LIMIT))
    limit = max(1, min(limit, MAX_LIMIT))

    clean = {"action": action, "doctype": doctype, "filters": filters, "limit": limit,
              "note": plan.get("note") or ""}
    if action in ("list", "get"):
        clean["fields"] = _validate_fields(doctype, plan.get("fields"))
        clean["order_by"] = _validate_order_by(doctype, plan.get("order_by"))
    return clean


# ---------------------------------------------------------------------------
# Execution — always as the current user, never ignore_permissions
# ---------------------------------------------------------------------------

def execute_plan(plan, user=None):
    user = user or frappe.session.user
    action = plan["action"]

    if action == "unsupported":
        return {"action": "unsupported", "note": plan.get("note")}

    if action == "create_draft":
        # Nothing is written yet — this is the proposal the user approves/cancels client-side.
        return {"action": "create_draft", "doctype": plan["doctype"], "values": plan["values"], "note": plan.get("note")}

    doctype = plan["doctype"]
    filters = plan["filters"]

    # No ignore_permissions, no user override: this runs as frappe.session.user,
    # so frappe.get_list applies that user's permission query conditions as usual.
    if action == "count":
        rows = frappe.get_list(doctype, filters=filters, fields=["count(name) as total"])
        total = rows[0]["total"] if rows else 0
        return {"action": "count", "doctype": doctype, "total": cint(total), "note": plan.get("note")}

    rows = frappe.get_list(
        doctype, filters=filters, fields=plan["fields"],
        order_by=plan["order_by"], limit_page_length=plan["limit"],
    )
    return {"action": action, "doctype": doctype, "fields": plan["fields"], "rows": rows,
            "count": len(rows), "note": plan.get("note")}


# ---------------------------------------------------------------------------
# Logging (audit trail — never blocks the response if it fails)
# ---------------------------------------------------------------------------

def _log(question, plan, outcome, error=None):
    try:
        frappe.get_doc({
            "doctype": "DI Copilot Log",
            "question": question[:500] if question else "",
            "action": (plan or {}).get("action") or "",
            "target_doctype": (plan or {}).get("doctype") or "",
            "plan": json.dumps(plan, default=str)[:4000] if plan else "",
            "result_count": cint(outcome.get("count") or outcome.get("total") or 0) if outcome else 0,
            "success": 0 if error else 1,
            "error": str(error)[:300] if error else "",
            "timestamp": frappe.utils.now_datetime(),
        }).insert(ignore_permissions=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- background audit log write, same pattern as _log_provider_call
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public entry points (called from the whitelisted API)
# ---------------------------------------------------------------------------

def ask(question, user=None):
    user = user or frappe.session.user
    if not question or not question.strip():
        raise CopilotError("Please type a question.")
    plan = raw_plan = None
    try:
        raw_plan = build_query_plan(question, user)
        plan = validate_plan(raw_plan, user)
        result = execute_plan(plan, user)
        result["plan"] = plan
        _log(question, plan, result)
        return result
    except CopilotError as e:
        _log(question, plan or raw_plan, None, error=e)
        raise
    except frappe.PermissionError as e:
        _log(question, plan or raw_plan, None, error=e)
        raise CopilotError("You don't have permission to run that query.")


def confirm_create(doctype, values, user=None):
    """The explicit 'Approve' step for a create_draft — the only place this
    module actually writes to the database, and only as the current user."""
    user = user or frappe.session.user
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except Exception:
            raise CopilotError("Invalid field values.")
    validated = validate_plan({"action": "create_draft", "doctype": doctype, "values": values}, user)
    doc = frappe.get_doc({"doctype": validated["doctype"], **validated["values"]})
    doc.insert()  # no ignore_permissions: respects the current user's create permission and mandatory-field validation
    return {"doctype": doc.doctype, "name": doc.name}
