# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Put the site into a known state before a load run, and prove nothing can talk to the outside world.

Runs INSIDE the backend container against the live site:

    bench-python tatva_connect/tests/partner_api_load/preflight.py check
    bench-python tatva_connect/tests/partner_api_load/preflight.py off
    bench-python tatva_connect/tests/partner_api_load/preflight.py restore

`off` snapshots every automation toggle to reports/toggles.snapshot.json and disables all of them
except the two the run must exercise: file screening (ClamAV) and file privacy (fail-closed, never
turned off). `restore` puts the snapshot back verbatim, so the operator's own switch settings survive
the test.

`check` is the gate. It refuses the run unless every egress channel is dead, and it never repairs
anything: an armed channel is the operator's call, not the harness's.
"""
import json
import sys
from pathlib import Path

import frappe

from tatva_connect.tests.partner_api_load.config import PARTNER_USER

SNAPSHOT = Path(__file__).resolve().parent / "reports" / "toggles.snapshot.json"

# Turned off for a run: the ones that reach outside the machine, or that write noise the test would
# then have to explain. Everything else stays exactly as the operator set it.
#
# What is NOT here matters more than what is. Dedup, the rate limiter, the task guards, file privacy
# and file screening are the behaviours under test: disabling them would not make the run safer, it
# would make it meaningless. Lead::CRM Lead::dedup in particular gates the routing normalisation the
# dedup anchor keys on -- without it duplicate leads slip through, and the test would report a defect
# that the test itself created.
TURN_OFF = (
	"Telephony::Channel::calls",     # outbound: places calls
	"Location::Google::capture",     # outbound: geocodes against Google
	"Storage::Azure::offload",       # outbound: ships bytes to blob storage
	"Task::Review::mirror",          # outbound: posts a review verdict webhook
)


def _toggles():
	return frappe.get_all("CRM Tatva Automation", fields=["name", "enabled"], order_by="name")


def egress_report():
	"""Every channel that could leave this machine, and whether it is armed.

	A toggle that is on but unconfigured is dead: a blank setting reads as disabled. That is the
	platform invariant, so it is what this reports -- armed means armed, not merely switched on.
	"""
	rows = []

	def add(channel, armed, why):
		rows.append({"channel": channel, "armed": armed, "detail": why})

	if not frappe.db.exists("DocType", "CRM WATI Settings"):
		add("WhatsApp (WATI)", False, "doctype absent")
	else:
		on = bool(frappe.db.get_single_value("CRM WATI Settings", "enabled"))
		add("WhatsApp (WATI)", on, "CRM WATI Settings.enabled")

	mail = frappe.get_all("Email Account", filters={"enable_outgoing": 1}, pluck="name")
	add("Outgoing email", bool(mail), f"{len(mail)} outgoing account(s)")

	hooks = frappe.get_all("Webhook", pluck="name") if frappe.db.exists("DocType", "Webhook") else []
	add("Frappe webhooks", bool(hooks), f"{len(hooks)} webhook(s)")

	if frappe.db.exists("DocType", "CRM Tatva Automation Rule"):
		live = frappe.get_all("CRM Tatva Automation Rule", filters={"enabled": 1}, pluck="name")
		add("Automation rules", bool(live), f"{len(live)} enabled rule(s)")
	else:
		add("Automation rules", False, "rule doctype absent")

	key = frappe.db.get_single_value("Google Settings", "api_key") if frappe.db.exists("DocType", "Google Settings") else None
	add("Google (location capture)", bool(key), "Google Settings.api_key" + ("" if key else " is blank"))

	tel = None
	if frappe.db.exists("DocType", "CRM Telephony Settings"):
		meta = frappe.get_meta("CRM Telephony Settings")
		tel = any(frappe.db.get_single_value("CRM Telephony Settings", f.fieldname)
		          for f in meta.fields if f.fieldtype in ("Data", "Password"))
	add("Telephony (Acefone)", bool(tel), "CRM Telephony Settings" + ("" if tel else " is unconfigured"))

	azure = frappe.conf.get("azure_storage_connection_string") or frappe.conf.get("azure_connection_string")
	add("Azure Blob offload", bool(azure), "connection string" + (" present" if azure else " absent"))

	return rows


def cmd_check():
	print("=== egress ===")
	rows = egress_report()
	width = max(len(r["channel"]) for r in rows)
	for r in rows:
		mark = "ARMED" if r["armed"] else "dead "
		print(f"  [{mark}] {r['channel']:<{width}}  {r['detail']}")

	armed = [r["channel"] for r in rows if r["armed"]]

	print()
	print("=== clamav (file screening must be live) ===")
	screening = frappe.db.get_value("CRM Tatva Automation", "Storage::File::screening", "enabled")
	host = frappe.db.get_single_value("CRM File Screening Settings", "clamav_host") \
		if frappe.db.exists("DocType", "CRM File Screening Settings") else None
	print(f"  screening toggle : {'on' if screening else 'OFF'}")
	print(f"  clamav host      : {host or '(unset)'}")

	print()
	print("=== automation toggles ===")
	on = [t.name for t in _toggles() if t.enabled]
	print(f"  {len(on)} on: {', '.join(on) if on else '(none)'}")

	print()
	if armed:
		print(f"REFUSING: {len(armed)} egress channel(s) armed -> {', '.join(armed)}")
		print("Disable them and re-run. The harness does not disarm egress for you.")
		return 1
	if not screening:
		print("REFUSING: file screening is off, so ClamAV would never be exercised.")
		return 1
	print("CLEAR: no egress channel is armed, and screening is live.")
	return 0


def cmd_off():
	SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
	before = {t.name: int(t.enabled) for t in _toggles()}
	SNAPSHOT.write_text(json.dumps(before, indent=2) + "\n")
	print(f"snapshot written: {SNAPSHOT}  ({sum(before.values())} on / {len(before)})")

	changed = []
	for name in TURN_OFF:
		if before.get(name):
			frappe.db.set_value("CRM Tatva Automation", name, "enabled", 0)
			changed.append(name)
	frappe.db.commit()

	for name in changed:
		print(f"  off  {name}")
	print(f"{len(changed)} toggle(s) turned off; {sum(before.values()) - len(changed)} left on as the operator set them.")
	print("kept on (these are what the run tests): dedup, rate limit, idempotency, task guards, "
	      "file privacy, file screening")
	return 0


def cmd_restore():
	if not SNAPSHOT.exists():
		print(f"no snapshot at {SNAPSHOT} -- nothing to restore")
		return 1
	before = json.loads(SNAPSHOT.read_text())
	restored = 0
	for name, was_on in before.items():
		if not frappe.db.exists("CRM Tatva Automation", name):
			continue
		if int(frappe.db.get_value("CRM Tatva Automation", name, "enabled")) != int(was_on):
			frappe.db.set_value("CRM Tatva Automation", name, "enabled", int(was_on))
			restored += 1
	frappe.db.commit()
	print(f"restored {restored} toggle(s) to the snapshot ({sum(before.values())} on / {len(before)})")
	return 0


SETTINGS = "CRM Partner API Settings"
QUOTA_SNAPSHOT = Path(__file__).resolve().parent / "reports" / "quota.snapshot.json"

# The daily record quota is sized for a partner's steady-state traffic, not for a bulk backfill of
# every activity a thousand patients ever had. Raising it for the run is a config change an operator
# would make for exactly this, and it is snapshotted and restored. The CALL rate is left alone: the
# paced run is meant to stay inside it, and the stress run is meant to be refused by it.
QUOTA_FIELDS = ("per_token_write_records", "global_write_records",
                "per_token_read_records", "global_read_records")


def cmd_quota():
	QUOTA_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
	before = {f: frappe.db.get_single_value(SETTINGS, f) for f in QUOTA_FIELDS}
	QUOTA_SNAPSHOT.write_text(json.dumps(before, indent=2) + "\n")
	for field in QUOTA_FIELDS:
		frappe.db.set_single_value(SETTINGS, field, 1_000_000)
	frappe.db.commit()
	print(f"record quotas raised to 1,000,000 for the run (was {before})")
	print(f"snapshot: {QUOTA_SNAPSHOT}")
	print("the per-minute CALL rate is untouched: paced stays inside it, stress gets refused by it.")
	return 0


def cmd_quota_restore():
	if not QUOTA_SNAPSHOT.exists():
		print("no quota snapshot -- nothing to restore")
		return 1
	before = json.loads(QUOTA_SNAPSHOT.read_text())
	for field, value in before.items():
		frappe.db.set_single_value(SETTINGS, field, value)
	frappe.db.commit()
	print(f"record quotas restored: {before}")
	return 0


# Live service-account logins are operator config, never source (public repo) — see config.PARTNER_USER.
PARTNERS = tuple(PARTNER_USER.values())


def cmd_wipe():
	"""Delete what the partner API wrote, and the idempotency keys that remember it.

	Scoped by owner: these grains also carry leads and tasks from earlier migration trials and from
	ordinary CRM use, owned by real people. Those are not ours and are never touched.

	The keys matter as much as the rows. A key stores the RESPONSE of the call that first used it, so
	deleting the leads but leaving the keys means the next run replays a stored response naming a lead
	that no longer exists — the API is right to replay it, and the run then fails on a lead it thinks
	it just created. Set-based, not doc-by-doc: 26k delete_doc calls exhaust the connection.
	"""
	holes = ", ".join(["%s"] * len(PARTNERS))
	before = {dt: frappe.db.count(dt) for dt in
	          ("CRM Lead", "CRM Task", "CRM Visit Audit", "CRM Call Log", "CRM Partner API Idempotency")}

	frappe.db.sql(f"""DELETE va FROM `tabCRM Visit Audit` va
	                  JOIN `tabCRM Task` t ON t.name = va.task
	                  WHERE t.owner IN ({holes})""", PARTNERS)
	frappe.db.sql(f"""DELETE f FROM `tabFile` f JOIN `tabCRM Task` t ON t.name = f.attached_to_name
	                  WHERE f.attached_to_doctype = 'CRM Task' AND t.owner IN ({holes})""", PARTNERS)
	frappe.db.sql(f"""DELETE f FROM `tabFile` f JOIN `tabCRM Lead` l ON l.name = f.attached_to_name
	                  WHERE f.attached_to_doctype = 'CRM Lead' AND l.owner IN ({holes})""", PARTNERS)
	for table in ("tabCRM Call Log", "tabCRM Task", "tabCRM Lead"):
		frappe.db.sql(f"DELETE FROM `{table}` WHERE owner IN ({holes})", PARTNERS)
	frappe.db.sql(f"DELETE FROM `tabCRM Partner API Idempotency` WHERE partner IN ({holes})", PARTNERS)
	frappe.db.commit()

	after = {dt: frappe.db.count(dt) for dt in before}
	print("deleted (written by the partner API):")
	for dt in before:
		print(f"  {dt:<32} {before[dt]:>7,} -> {after[dt]:>7,}   (-{before[dt] - after[dt]:,})")
	return 0


COMMANDS = {"check": cmd_check, "off": cmd_off, "restore": cmd_restore,
            "quota": cmd_quota, "quota-restore": cmd_quota_restore, "wipe": cmd_wipe}


def main():
	if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
		print(f"usage: preflight.py [{'|'.join(COMMANDS)}]")
		return 2
	frappe.init(site="dev.localhost")
	frappe.connect()
	try:
		return COMMANDS[sys.argv[1]]()
	finally:
		frappe.destroy()


if __name__ == "__main__":
	sys.exit(main())
