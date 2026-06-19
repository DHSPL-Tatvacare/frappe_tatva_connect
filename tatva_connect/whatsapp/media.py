"""WhatsApp media <-> File-on-Lead. WhatsApp-specific bits only (filename + the idempotency key);
all File creation / lookup / re-homing is delegated to the single storage.file_manager layer, so the
privacy + Azure + URL rules are never re-implemented here. Every WhatsApp media file ends in the SAME
state: attached to the CRM Lead, private, in Azure, stamped with the provider message id, and the WhatsApp
Message's `attach` points at the File's proxy URL."""
import os

from tatva_connect.storage import file_manager

_MEDIA_TYPES = {"image", "document", "video", "audio"}


def media_filename(media_type: str, text: str | None, data: str) -> str:
	"""Human filename for the File row. For documents WATI puts the ORIGINAL
	filename in `text`; for images `text` is a caption, so fall back to the uuid
	in the `data` path. Always keep a real extension."""
	if media_type == "document" and text:
		return text.strip()
	base = os.path.basename(data.split("?")[0]) or "file"  # '<uuid>.<ext>'
	return base


def find_lead_media(lead: str, message_uid: str):
	"""Existing File for this WATI message on this lead, or None (idempotency key)."""
	if not message_uid:
		return None
	return file_manager.find(
		attached_to_doctype="CRM Lead", attached_to_name=lead, custom_wa_message_id=message_uid
	)


def ensure_lead_media(lead: str, message_uid: str, filename: str, content: bytes):
	"""Idempotently create (or return) the File for this WATI media on the lead. Saved PRIVATE +
	attached to the LEAD -> the privacy policy keeps it private and the File offloads to Azure."""
	return find_lead_media(lead, message_uid) or file_manager.save(
		content,
		filename=filename,
		attached_to_doctype="CRM Lead",
		attached_to_name=lead,
		meta={"custom_wa_message_id": message_uid},
	)


def adopt_outbound_media(file_url: str, lead: str, message_uid: str):
	"""Outbound: the agent uploaded a File via the CRM tab (unattached). After the send succeeds,
	re-home it onto the lead (private, stamped with the provider message id, blob re-keyed into the lead folder).
	Returns the File's (possibly new) proxy URL. Idempotent."""
	fd = file_manager.find(file_url=file_url)
	if not fd:
		return file_url
	return file_manager.rehome(fd, "CRM Lead", lead, meta={"custom_wa_message_id": message_uid})
