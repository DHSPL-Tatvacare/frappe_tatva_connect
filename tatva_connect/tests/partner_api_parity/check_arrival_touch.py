"""Prove the arrival touch over the REAL HTTP partner API (local harness).

    python3 check_arrival_touch.py [account]        # default: tatvapractice

Nothing in-process: every fact below is asked of the running site through the same gated endpoints a
partner integration calls, and every assertion reads the API's OWN read-back, never the database.

SUCCESS CRITERIA — all five must hold, or the script exits 1.

  1. A first arrival is timed.        lead_create on an unknown phone returns `created`, and the lead
                                      reads back with exactly ONE acquisition row carrying a touch_at.
  2. A return is a second arrival.    lead_create on the SAME phone returns `updated` and the SAME lead,
                                      which now reads back with TWO rows, the newest later than the first.
  3. An edit is not an arrival.       lead_update by name changes a field and leaves the row count at two.
  4. A caller's own time is kept.     lead_create carrying its own touch_at stores THAT time, rather than
                                      being re-timed as if the patient had just arrived.
  5. The same time never arrives      sending that same touch_at AGAIN adds nothing — the guarantee a
     twice.                           Facebook re-crawl rests on, or one submission would arrive forever.

Each run mints its own phone, so it can be run repeatedly without a clean-up step deciding the verdict.
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from client import PartnerClient  # noqa: E402

CREDS = HERE.parents[2] / "docs" / "go-live" / "7-migrate-data" / ".creds" / "partner-api-tokens.local.json"
GRAIN_KEY = {"anaya": "anaya", "tatvapractice": "tp", "niva_bupa": "niva"}

PHONE = f"+916{int(time.time()) % 1_000_000_000:09d}"   # its own patient per run, so no clean-up decides the verdict
SENT_TOUCH = "2026-07-20 10:00:00"
TABLE = "custom_acquisition_profile"
COLUMN = "touch_at"

results = []


def check(name, ok, detail=""):
	results.append((name, bool(ok), detail))
	print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
	return bool(ok)


def token(account):
	users = json.loads(CREDS.read_text())["users"]
	key = GRAIN_KEY[account]
	for user, info in users.items():
		if key in user:
			return info["token"]
	raise SystemExit(f"no local token for {account}")


def touches(client, name):
	"""The acquisition times the API itself reports for this lead, oldest first."""
	body, code, _ms = client.lead_get(name=name)
	if code != 200:
		raise SystemExit(f"lead_get answered {code}: {json.dumps(body)[:300]}")
	rows = ((body.get("data") or {}).get(TABLE)) or []
	return sorted(row.get(COLUMN) for row in rows if row.get(COLUMN))


def arrive(client, payload):
	"""One arrival, as a partner makes it. Returns (http status, the action the API reports, the lead id)."""
	body, code, _ms = client.lead_create(payload)
	body = body if isinstance(body, dict) else {}
	return code, body.get("action"), (body.get("data") or {}).get("name")


def main():
	account = sys.argv[1] if len(sys.argv) > 1 else "tatvapractice"
	client = PartnerClient(token(account))
	print(f"\narrival touch over HTTP — account={account}, phone={PHONE}\n")
	patient = {"mobile_no": PHONE, "first_name": "Asha Arrival"}

	code, action, name = arrive(client, patient)
	first = touches(client, name) if name else []
	check("1. a first arrival is timed",
	      code == 200 and action == "created" and len(first) == 1,
	      f"http={code} action={action} touches={len(first)}")

	code, action, same = arrive(client, patient)
	returned = touches(client, name)
	check("2. a return is a second arrival",
	      code == 200 and action == "updated" and same == name
	      and len(returned) == 2 and returned[-1] > first[0],
	      f"http={code} action={action} same_lead={same == name} touches={len(returned)}")

	_body, code, _ms = client.lead_update({"name": name, "first_name": "Asha Corrected"})
	after_edit = touches(client, name)
	check("3. an edit is not an arrival", code == 200 and after_edit == returned,
	      f"http={code} touches={len(after_edit)}")

	code, _action, _same = arrive(client, {**patient, TABLE: [{COLUMN: SENT_TOUCH}]})
	with_sent = touches(client, name)
	check("4. a caller's own time is kept",
	      code == 200 and SENT_TOUCH in with_sent and len(with_sent) == 3,
	      f"http={code} kept={SENT_TOUCH in with_sent} touches={len(with_sent)}")

	code, _action, _same = arrive(client, {**patient, TABLE: [{COLUMN: SENT_TOUCH}]})
	resent = touches(client, name)
	check("5. the same time never arrives twice",
	      code == 200 and resent == with_sent,
	      f"http={code} touches={len(resent)}")

	failed = [n for n, ok, _d in results if not ok]
	print(f"\n{len(results) - len(failed)}/{len(results)} criteria met" + (f" — FAILED: {failed}" if failed else ""))
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
