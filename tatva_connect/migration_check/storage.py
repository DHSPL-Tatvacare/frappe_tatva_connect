# TEMPORARY — migration reconciliation demo, remove before prod. See REMOVE-ME.md
"""Where run artifacts live: plain files, deliberately not a doctype.

A batch or totals run needs state that survives a restart. A doctype would give that, and would
also leave a table, a Module Def and a `modules.txt` line to unwind at teardown. Files under the
site's private directory survive restarts just as well, download as CSV without any extra plumbing,
and disappear with an `rm -rf` — which keeps removal of this whole tool a delete rather than a
migration.

Nothing here is web-served directly. The pages read through the guarded endpoints, so private
files stay private.
"""

import json
import pathlib

import frappe

FOLDER = "migration_check"


def _root() -> pathlib.Path:
	path = pathlib.Path(frappe.get_site_path("private", "files", FOLDER))
	path.mkdir(parents=True, exist_ok=True)
	return path


def _safe(name: str) -> str:
	"""Run ids are built by this module, never by a caller — but never trust a path anyway."""
	cleaned = "".join(c for c in name if c.isalnum() or c in "-_")
	if not cleaned:
		frappe.throw("Invalid run id.", frappe.ValidationError)
	return cleaned


def path_for(run_id: str) -> pathlib.Path:
	return _root() / f"{_safe(run_id)}.json"


def write(run_id: str, payload: dict) -> None:
	"""Atomic replace, so a reader never sees a half-written run."""
	target = path_for(run_id)
	tmp = target.with_suffix(".json.tmp")
	tmp.write_text(json.dumps(payload, indent=1, default=str))
	tmp.replace(target)


def read(run_id: str) -> dict | None:
	target = path_for(run_id)
	if not target.exists():
		return None
	try:
		return json.loads(target.read_text())
	except (ValueError, OSError):
		return None


def listing(kind: str | None = None, limit: int = 25) -> list[dict]:
	"""Recent runs, newest first. `kind` filters on the id prefix ('totals' or 'batch')."""
	runs = []
	for path in _root().glob("*.json"):
		if kind and not path.stem.startswith(f"{kind}-"):
			continue
		try:
			data = json.loads(path.read_text())
		except (ValueError, OSError):
			continue
		runs.append(
			{
				"run_id": path.stem,
				"kind": data.get("kind"),
				"grain": data.get("grain"),
				"grain_label": data.get("grain_label"),
				"status": data.get("status"),
				"started_at": data.get("started_at"),
				"finished_at": data.get("finished_at"),
				"summary": data.get("summary") or {},
			}
		)
	runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
	return runs[:limit]


# -- live progress ------------------------------------------------------------
#
# A running batch updates a counter in Redis after every lead, and rewrites the full run file only
# periodically. The page polls the counter, so following a run costs a single cache read rather
# than parsing a file that grows with every lead.

PROGRESS_TTL = 3600


def _progress_key(run_id: str) -> str:
	return f"migration_check:progress:{_safe(run_id)}"


def progress(run_id: str, done: int, total: int, summary: dict) -> None:
	frappe.cache().set_value(
		_progress_key(run_id),
		{"done": done, "total": total, "summary": summary},
		expires_in_sec=PROGRESS_TTL,
	)


def read_progress(run_id: str) -> dict | None:
	return frappe.cache().get_value(_progress_key(run_id))


def clear_progress(run_id: str) -> None:
	frappe.cache().delete_value(_progress_key(run_id))


def to_csv(payload: dict) -> str:
	"""A run rendered as CSV — the thing an operator files or forwards."""
	import csv
	import io

	buf = io.StringIO()
	writer = csv.writer(buf)

	if payload.get("kind") == "totals":
		writer.writerow(["Record type", "LeadSquared", "Frappe", "Variance", "Note"])
		for row in payload.get("rows") or []:
			writer.writerow(
				[
					row.get("label"),
					row.get("lsq", ""),
					row.get("crm", ""),
					row.get("variance", ""),
					row.get("note", ""),
				]
			)
		return buf.getvalue()

	writer.writerow(
		[
			"ProspectID",
			"Frappe lead",
			"Verdict",
			"Activities LSQ",
			"Activities Frappe",
			"Notes LSQ",
			"Notes Frappe",
			"Calls LSQ",
			"Calls Frappe",
			"Detail",
		]
	)
	for row in payload.get("leads") or []:
		lsq = row.get("lsq") or {}
		crm = row.get("crm") or {}
		writer.writerow(
			[
				row.get("prospect_id"),
				row.get("lead_name") or "",
				row.get("verdict"),
				lsq.get("activities", ""),
				crm.get("activities", ""),
				lsq.get("notes", ""),
				crm.get("notes", ""),
				lsq.get("calls", ""),
				crm.get("calls", ""),
				"; ".join(row.get("reasons") or []),
			]
		)
	return buf.getvalue()
