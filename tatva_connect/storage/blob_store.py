# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Azure Blob backend for Frappe File storage.

Clean-room (principles, not code, shared with community Azure apps): a File's bytes
live in a private Azure Blob container; the File row keeps a permission-gated proxy
URL; downloads are served as short-lived SAS links. Credentials + the env/legacy
container come from `site_config` via `storage.location` (a property of the box, never
the DB); behavioural tunables (link validity, local-copy removal) live in `CRM Azure
Storage Settings`. Nothing is hardcoded here.

Auth today = connection string (account key) from site_config. Managed identity is a
later swap behind this same `BlobStore` seam.
"""

import mimetypes
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import frappe
from frappe import _

from tatva_connect import automation
from tatva_connect.storage import location

SETTINGS = "CRM Azure Storage Settings"
DOWNLOAD_METHOD = "tatva_connect.storage.api.download_file"
LOCAL_PREFIXES = ("/files/", "/private/files/")
_SAS_CACHE_PREFIX = "azure_blob_sas::"
_SAS_CACHE_SKEW = 30  # refresh the cached link this many seconds before it expires


def is_enabled() -> bool:
	"""Master switch — read fresh so a flip takes effect across workers at once."""
	return automation.is_enabled("Storage::Azure::offload")


def is_local_url(file_url: str | None) -> bool:
	"""True for a file still on local disk (not yet offloaded)."""
	return bool(file_url) and file_url.startswith(LOCAL_PREFIXES)


def download_url(blob_key: str) -> str:
	"""The permission-gated proxy URL stored on offloaded File rows. ROOT-RELATIVE (like Frappe's
	native /private/files URLs) so it always resolves to the host the user is browsing on. An absolute
	URL froze the upload-time host, so a private file uploaded under one host (e.g. localhost) 403'd
	as Guest when viewed under another (the site domain) — the session cookie is per-host."""
	return f"/api/method/{DOWNLOAD_METHOD}?file_name={blob_key}"


def blob_key_from_url(file_url: str | None) -> str | None:
	"""Inverse of `download_url`: pull the blob key out of a proxy URL."""
	if not file_url:
		return None
	return parse_qs(urlparse(file_url).query).get("file_name", [None])[0]


def _slug(value: str) -> str:
	"""Make a record name safe for one blob path segment (keep it legible, no nested
	folders): keep alnum/dot/dash/underscore, collapse the rest to '-'."""
	return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "rec"


class BlobStore:
	"""Thin wrapper over the Azure SDK: creds + container from `location` (site_config),
	behavioural tunables from the settings single.

	The SDK is imported lazily so merely importing this module never requires the
	package to be present (keeps app load + non-storage code paths clean).
	"""

	def __init__(self):
		self.settings = frappe.get_cached_doc(SETTINGS)
		self._service = None

	@property
	def service(self):
		if self._service is None:
			from azure.storage.blob import BlobServiceClient

			self._service = BlobServiceClient.from_connection_string(location.connection_string())
		return self._service

	def _container_for_key(self, blob_key: str) -> str:
		# One private container per BOX (the env); old keys route to the legacy container.
		# Bytes are NEVER world-readable in Azure — is_private still drives the download gate
		# (api.download_file). Fail-closed: an old key with no legacy container does no Azure op.
		if location.is_new_scheme(blob_key):
			return location.env()
		legacy = location.legacy_container()
		if not legacy:
			frappe.throw(_("No storage container resolvable for this file in this environment."))
		return legacy

	def new_key(
		self, file_name: str, attached_to_doctype: str | None, attached_to_name: str | None = None
	) -> str:
		"""Collision-proof blob key, grouped so the container browses sensibly:
		`<app>/<owner_doctype>/<owner_id>/<hash>_<name>`, where app + owner come from the
		walk-up resolver (an email attachment lands in its lead's folder). Unresolvable ->
		`platform/_unattached/<hash>_<name>`. The short hash keeps same-named files apart."""
		name = frappe.scrub(file_name) or "file"
		tag = frappe.generate_hash(length=10)
		owner_dt, owner_nm, app = location.resolve_owner(attached_to_doctype, attached_to_name)
		if owner_dt and owner_nm:
			return "/".join([app, frappe.scrub(owner_dt), _slug(owner_nm), f"{tag}_{name}"])
		return "/".join([location.DEFAULT_APP, "_unattached", f"{tag}_{name}"])

	# --- operations (only PRIVATE files are ever offloaded; one private container) ---
	def upload(self, blob_key: str, content: bytes, file_name: str) -> str:
		from azure.storage.blob import ContentSettings

		content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
		self._blob(blob_key).upload_blob(
			content, overwrite=True, content_settings=ContentSettings(content_type=content_type)
		)
		return download_url(blob_key)

	def download(self, blob_key: str) -> bytes:
		return self._blob(blob_key).download_blob().readall()

	def exists(self, blob_key: str) -> bool:
		"""True if the blob is present. Used to CONFIRM an upload before any local copy is
		ever removed — no local bytes are dropped until Azure says the blob is really there."""
		return bool(self._blob(blob_key).exists())

	def delete(self, blob_key: str):
		"""Delete a blob (and its snapshots). Idempotent — an already-absent blob is
		treated as success, so a re-delete never raises."""
		from azure.core.exceptions import ResourceNotFoundError

		try:
			self._blob(blob_key).delete_blob(delete_snapshots="include")
		except ResourceNotFoundError:
			pass

	def sas_url(self, blob_key: str) -> str:
		"""A short-lived read link, cached until just before it expires."""
		container = self._container_for_key(blob_key)
		cache_key = f"{_SAS_CACHE_PREFIX}{container}::{blob_key}"
		cached = frappe.cache().get_value(cache_key)
		if cached:
			return cached

		from azure.storage.blob import BlobSasPermissions, generate_blob_sas

		ttl = int(self.settings.sas_ttl_seconds or 900)
		token = generate_blob_sas(
			account_name=self.service.account_name,
			container_name=container,
			blob_name=blob_key,
			account_key=self.service.credential.account_key,
			permission=BlobSasPermissions(read=True),
			expiry=frappe.utils.get_datetime_in_timezone("UTC") + timedelta(seconds=ttl),
		)
		url = f"{self._blob(blob_key).url}?{token}"
		frappe.cache().set_value(cache_key, url, expires_in_sec=max(ttl - _SAS_CACHE_SKEW, _SAS_CACHE_SKEW))
		return url

	# --- internals ---
	def _blob(self, blob_key: str):
		container = self._container_for_key(blob_key)
		self._ensure_container(container)
		return self.service.get_blob_client(container=container, blob=blob_key)

	def _ensure_container(self, name: str):
		from azure.core.exceptions import ResourceExistsError

		try:
			self.service.get_container_client(name).create_container()
		except ResourceExistsError:
			pass
