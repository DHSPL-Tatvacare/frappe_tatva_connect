import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

# Both attach paths stage an UNATTACHED file here until the mail is sent: it shows as a
# composer chip, attaches to the mail on send, is deleted on discard — and because it's
# unattached it never appears on the lead (no Attachments-tab / activity noise) and never
# appears in the "from CRM" picker. Swept if a compose is abandoned.
DRAFT_FOLDER = "Home/Email Drafts"
DRAFT_TTL_HOURS = 24


def ensure_draft_folder():
	"""Create the staging folder if absent (runs on migrate; idempotent)."""
	if not frappe.db.exists("File", DRAFT_FOLDER):
		frappe.get_doc(
			{"doctype": "File", "file_name": "Email Drafts", "is_folder": 1, "folder": "Home"}
		).insert(ignore_permissions=True)


@frappe.whitelist()
def list_attachable_files(reference_doctype, reference_name):
	"""Real files attached to a CRM record, for the "from CRM" picker. Permission-scoped.
	Staged drafts are unattached, so they're naturally excluded."""
	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	return frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": reference_doctype,
			"attached_to_name": reference_name,
			"is_folder": 0,
		},
		fields=["name", "file_name", "file_url", "file_size", "is_private", "file_type"],
		order_by="creation desc",
	)


@frappe.whitelist()
def stage_crm_file(reference_doctype, reference_name, source_file):
	"""Stage an existing CRM file as an UNATTACHED draft (shares the blob — no re-upload) in
	the drafts folder, so it behaves exactly like a device upload: a composer chip that
	attaches on send and is deleted on discard WITHOUT touching the original (on_trash
	ref-counts the shared blob)."""
	from crm.api.whatsapp import validate_access

	validate_access(reference_doctype, reference_name)
	src = frappe.db.get_value(
		"File",
		{"name": source_file, "attached_to_doctype": reference_doctype, "attached_to_name": reference_name},
		["file_url", "is_private", "file_name", "custom_uploaded_to_azure"],
		as_dict=True,
	)
	if not src:
		frappe.throw(_("File does not belong to this record."), frappe.PermissionError)

	ensure_draft_folder()
	draft = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": src.file_name,
			"file_url": src.file_url,
			"is_private": src.is_private,
			"folder": DRAFT_FOLDER,
			"custom_uploaded_to_azure": src.custom_uploaded_to_azure or 0,
		}
	).insert(ignore_permissions=True)
	return {"name": draft.name, "file_url": draft.file_url, "file_name": draft.file_name}


def purge_draft_attachments():
	"""Daily: drop staged draft files left after a send or an abandoned compose. Folder-scoped
	and the on_trash ref-count keeps any shared blob alive for the sent copy / original."""
	cutoff = add_to_date(now_datetime(), hours=-DRAFT_TTL_HOURS)
	stale = frappe.get_all(
		"File",
		filters={"folder": DRAFT_FOLDER, "is_folder": 0, "creation": ["<", cutoff]},
		pluck="name",
	)
	for name in stale:
		try:
			frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
		except Exception:
			frappe.log_error(title="Email draft purge failed", message=f"file={name}")
	if stale:
		frappe.db.commit()
