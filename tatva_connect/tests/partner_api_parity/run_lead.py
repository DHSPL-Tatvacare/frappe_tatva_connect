"""Drive ONE lead over the real HTTP partner API (local harness).

  python3 run_lead.py <account> <prospect_id_prefix> [--activities]

Loads the lead from ../7-migrate-data/<account>/data/leads.jsonl, builds the API payload from
mapping.json, POSTs lead_create, reads it back, and (with --activities) POSTs each activity.
Writes reports/run_<pid8>.json. Assumes the lead was already targeted-deleted (clean-room).
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATE = HERE.parents[3] / "docs" / "go-live" / "7-migrate-data"  # pentest->live->tests->tatva_connect->repo
sys.path.insert(0, str(HERE))
import translate
from client import PartnerClient

# maps an account to the substring that identifies its partner user in the tokens file
_GRAIN_KEY = {"anaya": "anaya", "tatvapractice": "tp", "niva_bupa": "niva"}


def load_token(account):
    creds = json.loads((MIGRATE / ".creds" / "partner-api-tokens.local.json").read_text())
    key = _GRAIN_KEY[account]
    for user, info in creds["users"].items():
        if key in user:
            return info["token"]
    raise SystemExit(f"no token for {account}")


def find_lead(account, pid_prefix):
    for line in (MIGRATE / account / "data" / "leads.jsonl").open():
        r = json.loads(line)
        if (r.get("ProspectID") or "").startswith(pid_prefix):
            return r
    raise SystemExit(f"lead {pid_prefix} not in leads.jsonl")


def acts_for(account, pid):
    for line in (MIGRATE / account / "data" / "activities.jsonl").open():
        r = json.loads(line)
        if (r.get("RelatedProspectId") or "") == pid:
            yield r


def main():
    account, pid_prefix = sys.argv[1], sys.argv[2]
    do_acts = "--activities" in sys.argv
    c = PartnerClient(load_token(account))
    m = translate.load_mapping(account, MIGRATE)
    # multi-row children + their key field, from the live lead_schema (the real API contract)
    schema = json.loads((HERE / "schemas" / f"{account}_lead_schema.json").read_text())["data"]
    multi_row = {cf: spec.get("key_field") for cf, spec in schema.get("children", {}).items() if spec.get("multi_row")}
    rec = find_lead(account, pid_prefix)
    pid = rec["ProspectID"]
    payload, program, dropped = translate.lead_payload(rec, m, multi_row=multi_row)
    parents = [k for k in payload if not isinstance(payload[k], list)]
    kids = {k: len(v) for k, v in payload.items() if isinstance(payload[k], list)}
    print(f"=== lead_create {pid[:8]} program={program} parent_fields={len(parents)} children={kids}")
    for d in dropped:
        print(f"  DROPPED child {d['child']} ({d['fields_present']} fields): {d['reason']} [{d['key_field']}]")
    body, code, ms = c.lead_create(payload)
    ok = code == 200 and isinstance(body, dict) and body.get("status") == "success"
    print(f"  HTTP {code} {ms}ms: {json.dumps(body, default=str)[:300]}")
    result = {"account": account, "pid": pid, "program": program, "payload": payload,
              "dropped_children": dropped, "create": {"code": code, "ms": ms, "resp": body}}
    if not ok:
        print("  LEAD CREATE FAILED, stopping.")
        (HERE / "reports" / f"run_{pid[:8]}.json").write_text(json.dumps(result, indent=1, default=str))
        return
    lead_name = body["data"]["name"]
    rb, rbcode, _ = c.lead_get(name=lead_name)
    rbkids = {k: len(v) for k, v in rb.get("data", {}).items() if isinstance(v, list)} if isinstance(rb, dict) else {}
    print(f"  lead={lead_name}  lead_get HTTP {rbcode}  readback_children={rbkids}")
    result["lead_name"] = lead_name
    result["readback"] = rb
    if do_acts:
        acts = list(acts_for(account, pid))
        print(f"=== activities: {len(acts)} rows")
        ares, okc = [], 0
        for ap in translate.activity_payloads(acts, m, lead_name):
            b, cd, mm = c.activity_create(ap)
            aok = cd == 200 and isinstance(b, dict) and b.get("status") == "success"
            okc += aok
            ares.append({"task_type": ap["task_type"], "external_id": ap["external_id"],
                         "code": cd, "ms": mm, "ok": aok, "resp": b})
            tag = "OK" if aok else "FAIL " + json.dumps(b, default=str)[:180]
            print(f"  {ap['task_type'][:30]:30} ext={str(ap['external_id'])[:10]:10} {cd} {mm}ms {tag}")
        print(f"  activities OK {okc}/{len(ares)}")
        result["activities"] = ares
    (HERE / "reports" / f"run_{pid[:8]}.json").write_text(json.dumps(result, indent=1, default=str))
    print(f"  wrote reports/run_{pid[:8]}.json")


if __name__ == "__main__":
    main()
