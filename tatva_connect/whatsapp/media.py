"""WhatsApp media <-> File-on-Lead. WhatsApp-specific bits only (filename + the idempotency key);
all File creation / lookup / re-homing is delegated to the single storage.file_manager layer, so the
privacy + Azure + URL rules are never re-implemented here. Every WhatsApp media file ends in the SAME
state: attached to the CRM Lead, private, in Azure, stamped with the provider message id, and the WhatsApp
Message's `attach` points at the File's proxy URL."""
import os

from tatva_connect.storage import file_manager

_MEDIA_TYPES = {"image", "document", "video", "audio"}


def _plain_basename(text: str | None) -> str | None:
	"""`text` as a filename a human would recognise, or None if it is not one.

	Measured on live traffic, a DOCUMENT carries its original filename in `text` in BOTH dialects — the
	webhook and the v3 history agree. An image, video or audio carries no name anywhere: `text` is a
	caption on the webhook and null in v3, so a uuid is genuinely the only identifier those have.

	The one exception is an OUTBOUND row, where `text` holds the file's own URL. Filing
	"http://host/api/method/...download_file?file_name=..." as a filename is worse than a uuid, and
	salvaging its basename is worse still — that yields the METHOD name, with the real one left in the
	query string. Anything with a scheme or a path is refused outright.
	"""
	name = (text or "").strip()
	if not name or "://" in name or "/" in name:
		# A URL or a path is not a filename. Salvaging a basename out of one is worse than refusing:
		# our own proxy URL basenames to the METHOD name ("...api.download_file") and the real name
		# hides in the query string. Refuse, and let the provider name or the uuid answer instead.
		return None
	stem, ext = os.path.splitext(name)
	if not ext or not stem:
		return None
	return name


def media_filename(media_type: str, text: str | None, data: str, provider_name: str | None = None) -> str:
	"""Human filename for the File row — the ONE place a media file is named.

	Preference is human-first, and deliberately so. For a document WATI puts the ORIGINAL filename in
	`text` ("Batch Template Test.xlsx"); its OWN stored name is a uuid. Taking the provider's name first
	gave every attachment a uuid on screen while the real name sat unused in the message body.

	For an image `text` is a caption, not a name, so it is never used as one — those fall through to the
	provider's name and then to the uuid in the `data` path. A uuid is the fallback, never the default.
	"""
	human = _plain_basename(text) if media_type == "document" else None
	if human:
		return human
	if provider_name:
		return provider_name
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
		meta={"custom_wa_message_id": message_uid, "custom_source": "WhatsApp"},
	)


def adopt_outbound_media(file_url: str, lead: str, message_uid: str):
	"""Outbound: the agent uploaded a File via the CRM tab (unattached). After the send succeeds,
	re-home it onto the lead (private, stamped with the provider message id, blob re-keyed into the lead folder).
	Returns the File's (possibly new) proxy URL. Idempotent."""
	fd = file_manager.find(file_url=file_url)
	if not fd:
		return file_url
	return file_manager.rehome(fd, "CRM Lead", lead, meta={"custom_wa_message_id": message_uid})
