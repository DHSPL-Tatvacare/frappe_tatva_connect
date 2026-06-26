# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Render + persist the authz run (TESTS.md §11 artifacts).

Two outputs, no DB needed for either:
  render(results)         -> a compact fixed-width ascii grid (rows=principals, cols=doctype×action);
                             ESCALATION rows are flagged loud.
  to_json(results, conf)  -> dict for frappe.as_json; write(...) persists report + confusion JSON.
  summary_line(...)       -> one-line PASS/FAIL verdict per the §11 exit-0 criteria.

`results` = [{id|principal, doctype, action, verdict}] where verdict in {ALLOW, DENY, ESCALATION}.
Rendering is dependency-light so a test or the tcsec CLI can call it without a bench.
"""
REPORT_SUBDIR = "custom-user-work/security/reports"
REPORT_FILE = "authz-report.json"
CONFUSION_FILE = "authz-confusion.json"

# verdict -> (ascii marker, emoji). ESCALATION is the loud one.
_MARK = {
	"ALLOW": ("A", "🟢"),
	"DENY": ("D", "🛡️"),
	"ESCALATION": ("X", "🚨"),
}
_UNKNOWN = ("?", "⬜")


def _trunc(text, width):
	"""Compact-table rule: short strings padded; long strings -> first10..last20."""
	s = "" if text is None else str(text)
	if len(s) <= width:
		return s.ljust(width)
	if width <= 5:
		return s[:width]
	# project rule: first 10 + last 20 for long paths/names, clamped to the column width.
	head = s[:10]
	tail = s[-(width - len(head) - 2):]
	return (head + ".." + tail)[:width].ljust(width)


def _verdict(cell):
	return (cell.get("verdict") or "").upper()


def _row_key(cell):
	return cell.get("principal") or (cell.get("id") or "")


def _col_key(cell):
	dt = cell.get("doctype") or ""
	action = cell.get("action") or ""
	return (dt, action)


def _short_col(doctype, action):
	# CRM Lead/read -> "Lead·rd"; keep headers narrow.
	dt = (doctype or "").replace("CRM ", "").replace("WhatsApp ", "WA ")
	act = {"read": "rd", "write": "wr", "create": "cr", "delete": "del",
	       "field_read": "fld"}.get(action, action[:3])
	return "{0}·{1}".format(dt, act)


def render(results):
	"""Fixed-width grid string. Rows = principals, cols = doctype×action, cells = marker+emoji.

	Each cell shows the ascii marker; a legend maps marker->emoji. A trailing ESCALATIONS block
	lists every escalation row in full so the dangerous ones can never hide in a truncated cell.
	"""
	if not results:
		return "(no results)\n"

	rows, cols = [], []
	grid = {}
	for cell in results:
		r, c = _row_key(cell), _col_key(cell)
		if r not in rows:
			rows.append(r)
		if c not in cols:
			cols.append(c)
		grid[(r, c)] = cell

	row_w = max(12, *(len(str(r)) for r in rows)) if rows else 12
	row_w = min(row_w, 22)
	headers = [_short_col(dt, act) for dt, act in cols]
	col_w = max(7, *(len(h) for h in headers)) if headers else 7
	col_w = min(col_w, 14)

	lines = []
	head = " " * row_w + " | " + " | ".join(_trunc(h, col_w) for h in headers)
	lines.append(head)
	lines.append("-" * len(head))

	escalations = []
	for r in rows:
		body = []
		for c in cols:
			cell = grid.get((r, c))
			if cell is None:
				body.append(_trunc("", col_w))
				continue
			v = _verdict(cell)
			mark, _ = _MARK.get(v, _UNKNOWN)
			body.append(_trunc(mark, col_w))
			if v == "ESCALATION":
				escalations.append((r, c, cell))
		flag = "  🚨" if any(_verdict(grid.get((r, c)) or {}) == "ESCALATION" for c in cols) else ""
		lines.append(_trunc(r, row_w) + " | " + " | ".join(body) + flag)

	lines.append("")
	lines.append("legend: " + "  ".join(
		"{0}={1} {2}".format(m, e, name)
		for name, (m, e) in _MARK.items()
	) + "  ?=unknown")

	if escalations:
		lines.append("")
		lines.append("🚨 ESCALATIONS ({0}):".format(len(escalations)))
		for r, c, cell in escalations:
			lines.append("  🚨 {0}  [{1} {2}]  case={3}".format(
				r, c[0], c[1], cell.get("id") or "-"))

	return "\n".join(lines) + "\n"


def _counts(results):
	c = {"ALLOW": 0, "DENY": 0, "ESCALATION": 0, "unknown": 0}
	for cell in results:
		v = _verdict(cell)
		c[v if v in c else "unknown"] += 1
	return c


def to_json(results, confusion=None):
	"""Dict for frappe.as_json. Embeds the rendered grid, per-cell results, and the verdict."""
	counts = _counts(results)
	out = {
		"kind": "authz-report",
		"counts": counts,
		"escalations": [
			{"principal": _row_key(c), "doctype": c.get("doctype"),
			 "action": c.get("action"), "id": c.get("id")}
			for c in results if _verdict(c) == "ESCALATION"
		],
		"cells": [
			{"id": c.get("id"), "principal": _row_key(c), "doctype": c.get("doctype"),
			 "action": c.get("action"), "verdict": _verdict(c) or None}
			for c in results
		],
		"grid": render(results),
		"summary": summary_line(results, confusion),
	}
	if confusion is not None:
		out["confusion"] = _confusion_dict(confusion)
	return out


def _confusion_dict(confusion):
	"""Best-effort extraction from a Confusion object (built in parallel — degrade gracefully)."""
	if confusion is None:
		return None
	try:
		return confusion.as_dict()
	except Exception:
		# Confusion may not be fully built yet; fall back to whatever attrs exist.
		d = {}
		for attr in ("precision", "recall"):
			try:
				d[attr] = getattr(confusion, attr)
			except Exception:
				pass
		try:
			d["summary"] = confusion.summary()
		except Exception:
			pass
		return d or None


def _reports_dir():
	"""Resolve the reports dir: the repo checkout root, located via this file's own path.

	`custom-user-work/security/reports/` lives at the repo root, four levels above this module
	(tatva_connect/tests/authz/report.py). Anchoring on __file__ keeps it correct whether called
	from a bench test (cwd = bench root) or the tcsec CLI (cwd = repo root).
	"""
	import os
	repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
	return os.path.join(repo_root, REPORT_SUBDIR)


def _as_json(obj):
	try:
		import frappe
		return frappe.as_json(obj)
	except Exception:
		import json
		return json.dumps(obj, indent=1, default=str)


def write(results, confusion=None):
	"""Create the reports dir if missing; write report (+ confusion) JSON. Returns the paths."""
	import os
	out_dir = _reports_dir()
	os.makedirs(out_dir, exist_ok=True)

	payload = to_json(results, confusion)
	report_path = os.path.join(out_dir, REPORT_FILE)
	with open(report_path, "w") as fh:
		fh.write(_as_json(payload))

	paths = {"report": report_path}
	conf_dict = _confusion_dict(confusion)
	if conf_dict is not None:
		conf_path = os.path.join(out_dir, CONFUSION_FILE)
		with open(conf_path, "w") as fh:
			fh.write(_as_json(conf_dict))
		paths["confusion"] = conf_path

	return paths


def summary_line(results, confusion=None):
	"""One-line PASS/FAIL per §11 exit-0: no ESCALATION, no unknown drift, recall == 1.0."""
	counts = _counts(results)
	escalations = counts["ESCALATION"]
	unknowns = counts["unknown"]

	recall = None
	if confusion is not None:
		try:
			recall = float(confusion.recall)
		except Exception:
			recall = None

	reasons = []
	if escalations:
		reasons.append("{0} escalation(s) 🚨".format(escalations))
	if unknowns:
		reasons.append("{0} unresolved verdict(s)".format(unknowns))
	if recall is not None and recall < 1.0:
		reasons.append("recall {0:.2f} < 1.0".format(recall))

	total = len(results)
	if reasons:
		return "FAIL ({0}) — {1} cells, A={2} D={3} X={4}".format(
			"; ".join(reasons), total, counts["ALLOW"], counts["DENY"], escalations)

	recall_note = "recall 1.00" if recall is not None else "recall n/a (no confusion)"
	return "PASS — {0} cells, A={1} D={2}, no escalation, {3}".format(
		total, counts["ALLOW"], counts["DENY"], recall_note)
