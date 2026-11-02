# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A completed task printed as its form: frappe's own Print Format, preview and PDF, with the Letter Head picked by the task's grain."""
import frappe
from frappe import _
from frappe.utils import cstr
from frappe.www.printview import get_html_and_style

from tatva_connect.access import posture
from tatva_connect.activity.api import _settled, task_detail
from tatva_connect.lead import field_value
from tatva_connect.tasks.tasks import DONE_STATUS
from tatva_connect.taxonomy import grain

PRINT_FORMAT = "Task Form"
LETTER_HEAD = "Letter Head"
BLANK = "—"


def task_print_layout(doc):
	"""The task's title and its tabs → sections → columns of {label, value}, only the fields its form shows."""
	detail = task_detail(doc.name)
	cfg, task = detail["config"], detail["task"]
	if task["status"] != DONE_STATUS:
		frappe.throw(_("Only a completed task can be printed."), title=_("Not completed"))
	if not cfg:
		return {"title": task["title"], "tabs": []}
	values, files = task["values"], task["file_names"]
	shown, _live = _settled(cfg["fields"], values)
	by_name = {f.fieldname: f for f in cfg["fields"]}
	tabs = []
	for tab in cfg["tabs"]:
		sections = []
		for section in tab["sections"]:
			columns = [[_line(by_name[n], values.get(n), files) for n in column["fields"] if n in shown]
					   for column in section["columns"]]
			if columns := [c for c in columns if c]:
				sections.append({"label": section["label"], "columns": columns})
		if sections:
			tabs.append({"label": tab["label"], "sections": sections})
	return {"title": task["task_type_label"] or task["title"], "tabs": tabs}


def _line(f, value, files):
	"""One field as the label and the answer a person reads; an attachment by its file name."""
	if f.fieldtype in ("Attach", "Attach Image") and value:
		text = files.get(value) or value
	elif f.fieldtype == "Small Text":
		text = cstr(value)  # its line breaks are kept by the print's CSS, never turned into tags the escape would print
	else:
		text = field_value.as_text(f, value) if value not in (None, "", []) else ""
	return {"label": f.label, "value": text or BLANK}


def letter_head_for(task):
	"""The Letter Head whose grain most specifically covers the task's lead (else its type), or None for frappe's default."""
	row = frappe.db.get_value("CRM Task", task, ["reference_doctype", "reference_docname", "custom_task_type"], as_dict=True)
	axes = grain.of(row.reference_doctype, row.reference_docname) if row.reference_doctype == "CRM Lead" else ("", "", "")
	if not any(axes) and row.custom_task_type:
		axes = grain.own_of(frappe.get_cached_doc("CRM Task Type", row.custom_task_type))
	cols = {axis: col for axis, col in zip(grain.AXES, grain.own_columns(LETTER_HEAD), strict=True) if col}
	if not cols:
		return None
	rows = frappe.get_all(LETTER_HEAD, filters={"disabled": 0}, fields=["name", *cols.values()])  # authz-ok: tier-a — presentation config, read only after the task's own read gate
	# A Letter Head naming no grain is frappe's to pick (`is_default`), never a wildcard candidate here.
	candidates = [{"name": r.name, **{axis: r.get(col) for axis, col in cols.items()}} for r in rows if any(r.get(c) for c in cols.values())]
	hit = grain.resolve_scoped(candidates, *axes)
	return hit["name"] if hit else None


@frappe.whitelist(methods=["GET"])
def preview(task):
	"""The task's print preview as Desk draws it, and the Letter Head its PDF is made with."""
	posture.require("CRM Task", "read", doc=task)
	letterhead = letter_head_for(task)
	shown = get_html_and_style("CRM Task", str(task), PRINT_FORMAT, no_letterhead=0, letterhead=letterhead)
	# The engine is the format's own setting; `download_pdf` would otherwise force wkhtmltopdf over it.
	generator = frappe.db.get_value("Print Format", PRINT_FORMAT, "pdf_generator")
	return {"html": shown["html"], "style": shown["style"], "format": PRINT_FORMAT, "letterhead": letterhead, "pdf_generator": generator}
