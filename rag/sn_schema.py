"""
rag/sn_schema.py — Live ServiceNow schema validation via Table REST API.

Queries the connected ServiceNow instance to get real table/field definitions.
Used by the RAG server to inject verified schema context before generation,
preventing the model from hallucinating table names and field names.

Reads credentials from .env:
  SN_INSTANCE   — instance hostname (e.g. dev12345.service-now.com)
  SN_USERNAME   — basic auth username
  SN_PASSWORD   — basic auth password
  SN_TOKEN      — bearer token (alternative to basic auth)
"""

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SN_INSTANCE = os.getenv("SN_INSTANCE", "")
SN_USERNAME = os.getenv("SN_USERNAME", "")
SN_PASSWORD = os.getenv("SN_PASSWORD", "")
SN_TOKEN = os.getenv("SN_TOKEN", "")


class SNSchemaError(Exception):
    pass


def _client() -> httpx.Client:
    if not SN_INSTANCE:
        raise SNSchemaError("SN_INSTANCE not set in .env — schema validation disabled")

    base_url = f"https://{SN_INSTANCE}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    if SN_TOKEN:
        headers["Authorization"] = f"Bearer {SN_TOKEN}"
        return httpx.Client(base_url=base_url, headers=headers, timeout=15)
    elif SN_USERNAME and SN_PASSWORD:
        return httpx.Client(
            base_url=base_url,
            auth=(SN_USERNAME, SN_PASSWORD),
            headers=headers,
            timeout=15,
        )
    else:
        raise SNSchemaError("No SN credentials in .env — set SN_USERNAME+SN_PASSWORD or SN_TOKEN")


def is_configured() -> bool:
    """Return True if SN credentials are configured."""
    return bool(SN_INSTANCE and (SN_TOKEN or (SN_USERNAME and SN_PASSWORD)))


# ── Table validation ──────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def validate_table(table_name: str) -> dict:
    """
    Check whether a table exists on the instance.
    Returns {"exists": bool, "label": str, "parent": str}
    Cached per session.
    """
    if not is_configured():
        return {"exists": None, "label": None, "parent": None, "error": "not_configured"}

    try:
        client = _client()
        resp = client.get(
            "/api/now/table/sys_db_object",
            params={
                "sysparm_query": f"name={table_name}",
                "sysparm_fields": "name,label,super_class.name",
                "sysparm_limit": 1,
            },
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])
        if results:
            r = results[0]
            return {
                "exists": True,
                "label": r.get("label", ""),
                "parent": r.get("super_class.name", ""),
            }
        return {"exists": False, "label": None, "parent": None}
    except SNSchemaError as e:
        return {"exists": None, "error": str(e)}
    except Exception as e:
        return {"exists": None, "error": f"SN API error: {e}"}


# ── Field schema ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=128)
def get_table_fields(table_name: str) -> list[dict]:
    """
    Return all fields for a table from sys_dictionary.
    Cached per session.

    Each field: {"name": str, "label": str, "type": str, "reference": str}
    """
    if not is_configured():
        return []

    try:
        client = _client()
        resp = client.get(
            "/api/now/table/sys_dictionary",
            params={
                "sysparm_query": f"name={table_name}^element!=NULL",
                "sysparm_fields": "element,column_label,internal_type,reference",
                "sysparm_limit": 500,
            },
        )
        resp.raise_for_status()
        fields = []
        for r in resp.json().get("result", []):
            ref = r.get("reference", {})
            fields.append({
                "name": r.get("element", ""),
                "label": r.get("column_label", ""),
                "type": r.get("internal_type", ""),
                "reference": ref.get("value", "") if isinstance(ref, dict) else "",
            })
        return fields
    except Exception as e:
        return [{"error": str(e)}]


def validate_fields(table_name: str, field_names: list[str]) -> dict:
    """
    Check which field names exist on a table.
    Returns {"valid": [...], "invalid": [...], "table_exists": bool}
    """
    table_info = validate_table(table_name)
    if not table_info.get("exists"):
        return {
            "table_exists": False,
            "valid": [],
            "invalid": field_names,
        }

    fields = get_table_fields(table_name)
    known = {f["name"] for f in fields if "name" in f}
    return {
        "table_exists": True,
        "valid": [f for f in field_names if f in known],
        "invalid": [f for f in field_names if f not in known],
    }


# ── Schema context builder ────────────────────────────────────────────────────

# Patterns that look like ServiceNow table names
_TABLE_PATTERN = re.compile(
    r'\b((?:sn|sys|x|u|sc|wm|cmdb|sm|hr|csm|svc|fm|em|itom|itsm|sla|pa|'
    r'kb|cat|sn_aia|sn_gai|sn_genai|sn_nowassist|sn_occ)_[a-z][a-z0-9_]{2,})\b'
)


def extract_table_names(text: str) -> list[str]:
    """Extract probable ServiceNow table names from a query string."""
    found = list(dict.fromkeys(_TABLE_PATTERN.findall(text)))  # dedupe, preserve order
    return found


def build_schema_context(query: str) -> str:
    """
    Given a user query, detect table names, pull live schemas, and return
    a formatted context block to inject into the generation prompt.
    Returns empty string if SN is not configured or no tables found.
    """
    if not is_configured():
        return ""

    tables = extract_table_names(query)
    if not tables:
        return ""

    lines = ["## Live ServiceNow Schema (verified against connected instance)\n"]
    found_any = False

    for table in tables[:5]:  # cap at 5 tables per query
        info = validate_table(table)

        if info.get("exists") is None:
            continue  # SN unreachable — skip silently

        if not info.get("exists"):
            lines.append(f"### `{table}` — TABLE NOT FOUND ON THIS INSTANCE")
            lines.append(f"> Do not use `{table}` — it does not exist. Check the correct table name.\n")
            found_any = True
            continue

        fields = get_table_fields(table)
        field_lines = []
        for f in fields:
            if "error" in f:
                continue
            ref = f" → `{f['reference']}`" if f.get("reference") else ""
            field_lines.append(f"  - `{f['name']}` ({f['type']}){ref}  // {f['label']}")

        label = info.get("label", table)
        parent = f" extends `{info['parent']}`" if info.get("parent") else ""
        lines.append(f"### `{table}` — {label}{parent}")
        if field_lines:
            lines.append("\n".join(field_lines[:50]))  # cap at 50 fields
        else:
            lines.append("  *(no fields returned)*")
        lines.append("")
        found_any = True

    if not found_any:
        return ""

    lines.append("> Schema sourced live from connected ServiceNow instance. Use these exact field names.\n")
    return "\n".join(lines)
