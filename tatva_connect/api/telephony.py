"""Stream a call recording on demand — no storage.

The native Calls-tab player (via the `get_call_log` override in
`acefone/bridge.py`) points its `<audio>` at this endpoint. We never download or
store the audio; only the provider URL lives on the Call Log. This also avoids
crm's `get_recording_url`, which only supports Twilio/Exotel.
"""
import frappe


@frappe.whitelist()
def recording(call_log):
	"""Stream a call's recording through Frappe — nothing is written to disk.

	The browser's <audio> element hits this with the session cookie, so it runs
	as the logged-in agent; we check read permission on the Call Log, then fetch
	the provider URL (with the account's Bearer token if the URL is protected)
	and hand the bytes straight back. Pure pass-through, on play only.
	"""
	import requests
	from werkzeug.wrappers import Response

	doc = frappe.get_doc("CRM Call Log", call_log)
	if not frappe.has_permission("CRM Call Log", "read", doc):
		raise frappe.PermissionError("Not permitted to play this recording.")

	url = doc.recording_url
	if not url:
		frappe.throw("No recording on this call.")

	headers = {}
	account = doc.get("custom_acefone_account")
	if account:
		token = frappe.get_doc("CRM Acefone Account", account).get_password("api_token")
		if token:
			headers["Authorization"] = f"Bearer {token}"

	try:
		resp = requests.get(url, headers=headers, timeout=30)
		resp.raise_for_status()
	except Exception:
		frappe.log_error(title="Acefone recording fetch failed", message=frappe.get_traceback())
		frappe.throw("Could not fetch the recording from the provider.")

	content_type = resp.headers.get("Content-Type") or "audio/mpeg"
	frappe.local.response = Response(resp.content, mimetype=content_type)
