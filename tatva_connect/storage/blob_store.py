# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Azure Blob backend for Frappe File storage.

Clean-room (principles, not code, shared with community Azure apps): a File's bytes
live in a private Azure Blob container; the File row keeps a permission-gated proxy
URL; downloads are served as short-lived SAS links. Every tunable — credentials,
container names, link validity, local-copy removal — lives in `CRM Azure Storage
Settings`; nothing is hardcoded here.

Auth today = connection string (account key), stored encrypted on the settings
single. Managed identity is a later swap behind this same `BlobStore` seam.
"""

import mimetypes
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import frappe
from frappe import _

SETTINGS = "CRM Azure Storage Settings"
DOWNLOAD_METHOD = "tatva_connect.storage.api.download_file"
LOCAL_PREFIXES = ("/files/", "/private/files/")
_SAS_CACHE_PREFIX = "azure_blob_sas::"
_SAS_CACHE_SKEW = 30  # refresh the cached link this many seconds before it expires


def is_enabled() -> bool:
	"""Master switch — read fresh so a flip takes effect across workers at once."""
	return bool(frappe.db.get_single_value(SETTINGS, "enabled"))


def is_local_url(file_url: str | None) -> bool:
	"""True for a file still on local disk (not yet offloaded)."""
	return bool(file_url) and file_url.startswith(LOCAL_PREFIXES)


def download_url(blob_key: str) -> str:
	"""The permission-gated proxy URL stored on offloaded File rows."""
	return f"{frappe.utils.get_url()}/api/method/{DOWNLOAD_METHOD}?file_name={blob_key}"


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
	"""Thin wrapper over the Azure SDK, configured entirely from the settings single.

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

			conn = self.settings.get_password("connection_string")
			if not conn:
				frappe.throw(_("Set the Connection String in {0}.").format(SETTINGS))
			self._service = BlobServiceClient.from_connection_string(conn)
		return self._service

	@property
	def container(self) -> str:
		# One private container for everything: bytes are NEVER world-readable in Azure.
		# Frappe's own is_private flag still drives the download gate (see api.download_file);
		# it does not need to change where the bytes live.
		name = self.settings.private_container
		if not name:
			frappe.throw(_("Set the Private Container in {0}.").format(SETTINGS))
		return name

	def new_key(
		self, file_name: str, attached_to_doctype: str | None, attached_to_name: str | None = None
	) -> str:
		"""Collision-proof blob key, grouped so the container browses sensibly:
		`<doctype>/<record>/<hash>_<name>` when the file is attached to a record (one
		folder per lead etc.), else `<doctype>/<hash>/<name>` or `<hash>/<name>`. The
		short hash keeps same-named files in one record from clashing."""
		name = frappe.scrub(file_name) or "file"
		tag = frappe.generate_hash(length=10)
		if attached_to_doctype and attached_to_name:
			return "/".join([frappe.scrub(attached_to_doctype), _slug(attached_to_name), f"{tag}_{name}"])
		parts = [frappe.scrub(attached_to_doctype)] if attached_to_doctype else []
		parts += [tag, name]
		return "/".join(parts)

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

	def delete(self, blob_key: str):
		self._blob(blob_key).delete_blob(delete_snapshots="include")

	def sas_url(self, blob_key: str) -> str:
		"""A short-lived read link, cached until just before it expires."""
		cache_key = f"{_SAS_CACHE_PREFIX}{self.container}::{blob_key}"
		cached = frappe.cache().get_value(cache_key)
		if cached:
			return cached

		from azure.storage.blob import BlobSasPermissions, generate_blob_sas

		ttl = int(self.settings.sas_ttl_seconds or 900)
		token = generate_blob_sas(
			account_name=self.service.account_name,
			container_name=self.container,
			blob_name=blob_key,
			account_key=self.service.credential.account_key,
			permission=BlobSasPermissions(read=True),
			expiry=datetime.now(timezone.utc) + timedelta(seconds=ttl),
		)
		url = f"{self._blob(blob_key).url}?{token}"
		frappe.cache().set_value(cache_key, url, expires_in_sec=max(ttl - _SAS_CACHE_SKEW, _SAS_CACHE_SKEW))
		return url

	# --- internals ---
	def _blob(self, blob_key: str):
		self._ensure_container(self.container)
		return self.service.get_blob_client(container=self.container, blob=blob_key)

	def _ensure_container(self, name: str):
		from azure.core.exceptions import ResourceExistsError

		try:
			self.service.get_container_client(name).create_container()
		except ResourceExistsError:
			pass
