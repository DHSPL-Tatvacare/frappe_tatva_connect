# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Smart Setup form's endpoints: one stage per call, queued on the long lane and run as the person who pressed it.

The setup row is the truth and the realtime events are the fast path (the `exports.py` pattern): a tab that
missed an event reloads the row and reads the same answer. Which stage may start is decided once, here
(`next_stage`), and both the form's button and `start` ask it.
"""
from collections import Counter

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.background_jobs import is_job_enqueued

from tatva_connect.automation import settings as automation
from tatva_connect.smart_setup import bundle, recipes
from tatva_connect.taxonomy import grain

_TOGGLE = "Setup::SmartSetup::desk"  # dormant: nothing is built, checked or applied until an operator turns it on
DOCTYPE = "CRM Smart Setup"
EVENT_PROGRESS = "smart_setup_progress"
EVENT_REFRESH = "smart_setup_refresh"

# stage -> (direction, statuses it starts from, status while it runs, status when it passes, status when it fails)
_STAGES = {
	"build": ("Export", ("Draft", "Build Failed"), "Building", "Built", "Build Failed"),
	"check": ("Import", ("Draft", "Check Failed", "Apply Failed"), "Checking", "Checked", "Check Failed"),
	"apply": ("Import", ("Checked",), "Applying", "Applied", "Apply Failed"),
}
RUNNING = tuple(running for _direction, _starts, running, *_outcome in _STAGES.values())


def job_id(setup):
	"""The one background job a setup may have: a second press while it is queued is dropped."""
	return f"smart_setup::{setup}"


def is_running(doc):
	"""A stage's worker is alive; a running status whose job is gone was cut off, by a restart or a crash."""
	return doc.status in RUNNING and is_job_enqueued(job_id(doc.name))


def next_stage(doc):
	"""The one stage this setup can start now, or None; a stage whose worker was cut off may start again."""
	if doc.is_new() or is_running(doc):
		return None
	ready = {"build": bool(doc.roots), "check": bool(doc.bundle_file), "apply": bool(doc.bundle_file)}
	return next((stage for stage, (direction, starts, running, *_outcome) in _STAGES.items()
	             if doc.direction == direction and doc.status in (*starts, running) and ready[stage]), None)


@frappe.whitelist()
def recipe_options():
	"""Each recipe the form offers, the doctype its records are picked from, and whether it can be picked by grain."""
	frappe.has_permission(DOCTYPE, "read", throw=True)
	return [{"recipe": key, "root": r["root"], "by_grain": any(grain.columns(r["root"]))} for key, r in recipes.RECIPES.items()]


@frappe.whitelist()
def roots_for_grain(recipe, vertical=None, group=None, program=None):
	"""Every record of the recipe's root on this product line, group and program, as the reader may list them."""
	root = recipes.get(recipe)["root"]
	axes = {col: val for col, val in zip(grain.columns(root), (vertical, group, program), strict=True) if col and val}
	return frappe.get_list(root, filters=axes, pluck="name", order_by="name asc")


@frappe.whitelist()
def start(setup, stage):
	"""Queue one stage of a setup; the form watches its status and progress."""
	frappe.has_permission(DOCTYPE, "write", doc=setup, throw=True)
	automation.require(_TOGGLE, _("Smart Setup"))
	doc = frappe.get_doc(DOCTYPE, setup)
	if next_stage(doc) != stage:
		frappe.throw(_("This setup cannot {0} now. Reload the form.").format(_(stage)), title=_("Not ready"))
	doc.db_set({"status": _STAGES[stage][2], "error": None})
	frappe.enqueue("tatva_connect.smart_setup.api.run", queue="long", enqueue_after_commit=True,
	               job_id=job_id(setup), deduplicate=True, setup=setup, stage=stage)


def run(setup, stage):
	"""The worker: one stage, run as the person who pressed it, its outcome written back on the setup."""
	doc = frappe.get_doc(DOCTYPE, setup)
	passed, failed = _STAGES[stage][3:]
	try:
		if stage == "build":
			_build(doc)
		else:
			found = bundle.read(doc.bundle_text())
			_record(doc, (bundle.check if stage == "check" else bundle.apply)(found, _progress(doc)), passed, failed)
	except Exception as e:
		_fail(doc, failed, e)
	frappe.publish_realtime(EVENT_REFRESH, {"setup": doc.name}, user=frappe.session.user, after_commit=True)


def _build(doc):
	"""Write the bundle as a file on the setup, and what it holds onto the form."""
	text = bundle.build(doc.recipe, [row.record for row in doc.roots])
	found = frappe.parse_json(text)
	file = frappe.get_doc({
		"doctype": "File",
		"file_name": f"smart-setup-{frappe.scrub(doc.recipe)}-{doc.name}.json",
		"attached_to_doctype": DOCTYPE,
		"attached_to_name": doc.name,
		"attached_to_field": "bundle_file",
		"content": text,
		# `is_private` is NOT set here: `file_events.apply_privacy_policy` derives it. The caller never decides.
	}).save(ignore_permissions=True)  # authz-ok: tier-a — the setup's own artefact; the setup row is its owner and its gate
	doc.db_set({"bundle_file": file.file_url, "record_count": len(found["records"]), "source_site": found["source_site"],
	            "exported_at": found["exported_at"], "status": "Built"})
	frappe.db.commit()


def _record(doc, results, passed, failed):
	"""Every record's verdict on the setup, refused first, with the counts; one refused record fails the stage."""
	counts = Counter(r["action"] for r in results)
	doc.reload()
	doc.set("items", sorted(results, key=lambda r: r["action"] != bundle.REFUSED))
	doc.update({f"{action}_count": counts[action] for action in bundle.ACTIONS})
	doc.status = failed if counts[bundle.REFUSED] else passed
	doc.flags.ends_stage = True  # the stage writing its own end; every other save waits for it
	doc.save()
	frappe.db.commit()


def _progress(doc):
	"""The callback check and apply call per record: done so far, of how many."""
	def publish(done, total):
		frappe.publish_realtime(EVENT_PROGRESS, {"setup": doc.name, "done": done, "total": total}, user=frappe.session.user)

	return publish


def _fail(doc, failed, error):
	"""A stage that crashed: the reason on the form for the operator, the traceback in the Error Log for us."""
	frappe.db.rollback()
	frappe.log_error(title=f"Smart Setup {doc.name} failed", reference_doctype=DOCTYPE, reference_name=doc.name)
	doc.reload()
	doc.db_set({"status": failed, "error": cstr(error)[:500]})
	frappe.db.commit()
