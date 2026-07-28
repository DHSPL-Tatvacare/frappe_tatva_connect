"""Index the two remaining lead-timeline reads that scan: CRM Task and File, by (link, creation).

The Tasks and Attachments tabs are about to page server-side — filter on the lead, order by creation,
LIMIT a page. Measured with EXPLAIN on a dev site, BOTH abandon their existing index and fall back to a
full scan of the `creation` index, filtering rows as they walk:

  CRM Task  type=index  possible_keys=ix_refdoc_tasktype_status  key=creation
  File      type=index  possible_keys=NULL                       key=creation

CRM Task's `ix_refdoc_tasktype_status` leads with reference_docname but carries task_type/status next, so
it cannot serve `ORDER BY creation` and the optimizer drops it. File's index leads with
attached_to_doctype, and MariaDB cannot seek past a non-leading column when we filter on attached_to_name
alone — the same defect `add_lead_timeline_indexes` fixed for CRM Call Log.

Pairing the link column with `creation` serves the filter, the sort and the LIMIT from one seek. This is
the shape already shipped for FCRM Note and CRM Call Log; these are the last two tables missing it.

ADDITIVE. Both existing indexes stay — they serve other filters (task_type/status; attached_to_doctype).
"""
import frappe

INDEXES = (
	("CRM Task", "ix_task_refdoc_creation", ["reference_docname", "creation"]),
	("File", "ix_file_attachedname_creation", ["attached_to_name", "creation"]),
)


def execute():
	for doctype, index, fields in INDEXES:
		if frappe.db.has_index(f"tab{doctype}", index):
			continue
		try:
			frappe.db.add_index(doctype, fields, index)
		except Exception:
			frappe.log_error(
				title=f"{doctype} timeline paging index failed",
				message=frappe.get_traceback(),
			)
