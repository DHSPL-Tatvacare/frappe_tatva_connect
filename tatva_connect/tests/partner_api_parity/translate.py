"""Build a partner-API payload from an LSQ record, driven by ../7-migrate-data/<account>/mapping.json.

We send HUMAN values (raw LSQ text); the API resolves picklists/stages internally (that's the
resolution we're testing). Routing (source/vertical/group) is FORCED by the API, so we never send
it -- EXCEPT custom_current_program: Anaya is an open-program key, so the program is derived here
via the SAME rule the migration uses and sent per lead.
"""
import json
from pathlib import Path

# Targets the API forces from entitlement (never partner-writable). We must not send these.
_FORCED_TARGETS = {"source", "custom_vertical", "custom_group"}


def load_mapping(account, migrate_root):
    return json.loads((Path(migrate_root) / account / "mapping.json").read_text())


def resolve_program(rec, rule):
    """The lead's program from its own data, mirroring load._resolve_program_rule (by_source,
    drug value_map then drug presence_map). None if unmapped (lead stays program-less)."""
    if not rule:
        return None
    src = (rec.get("Source") or "").strip()
    if src in (rule.get("by_source") or {}):
        return rule["by_source"][src]
    by_drug = rule.get("by_drug") or {}
    for field, vmap in (by_drug.get("value_map") or {}).items():
        v = (rec.get(field) or "").strip()
        if v in vmap:
            return vmap[v]
    for field, prog in (by_drug.get("presence_map") or {}).items():
        if (rec.get(field) or "").strip():
            return prog
    return None


def lead_payload(rec, m, include_stage=False, multi_row=None):
    """(payload, program, dropped). Parent fields from lead_core (minus forced routing) + children
    arrays. Child rows are single-element lists (the migration writes one row per child).

    `multi_row` = {child_fieldname: key_field} from the live lead_schema. A multi-row child whose
    LSQ row lacks its key_field is DROPPED (never key-fabricated) and returned in `dropped`, so the
    rest of the lead still loads and the gap is reported instead of failing the whole record.
    Stage is opt-in (custom_substage may not be partner-writable); probe separately."""
    multi_row = multi_row or {}
    payload, dropped = {}, []
    for lsq, fr in m["lead_core"].items():
        if fr in _FORCED_TARGETS:
            continue
        v = rec.get(lsq)
        if v not in (None, ""):
            payload[fr] = v
    program = resolve_program(rec, m.get("program"))
    if program:
        payload["custom_current_program"] = program
    if include_stage and rec.get(m.get("stage_field", "ProspectStage")):
        payload["custom_substage"] = rec.get(m["stage_field"])  # raw human value; API resolves
    for cf, cm in m["children"].items():
        row = {fr: rec.get(lsq) for lsq, fr in cm["fields"].items() if rec.get(lsq) not in (None, "")}
        if not row:
            continue
        key_field = multi_row.get(cf)
        if key_field and row.get(key_field) in (None, ""):
            dropped.append({"child": cf, "key_field": key_field, "reason": "multi-row child missing its row key",
                            "fields_present": len(row)})
            continue
        payload[cf] = [row]
    return payload, program, dropped


def activity_payloads(rec_iter, m, lead_name):
    """Yield one activity_create payload per migratable activity row for a lead. Mirrors
    load.load_activities: event code to task_type (bare name), mx_Custom slots to schema fieldnames,
    external_id = ProspectActivityId (dedup), created_at = CreatedOn (activities CAN backdate)."""
    type_by_code = m.get("activity_task_types", {})
    fields_by_code = m.get("activity_fields", {})
    for rec in rec_iter:
        code = str(rec.get("_lsq_event_code") or rec.get("ActivityEvent"))
        type_name = type_by_code.get(code)
        aid = rec.get("ProspectActivityId")
        if not type_name or not aid:
            continue
        values = {}
        for slot, fieldname in fields_by_code.get(code, {}).items():
            v = rec.get(slot)
            if v not in (None, ""):
                values[fieldname] = v
        yield {
            "lead": lead_name,
            "task_type": type_name,          # bare name; API resolves to the grain-scoped composite type
            "external_id": aid,
            "values": values,
            "created_at": rec.get("CreatedOn"),
        }
