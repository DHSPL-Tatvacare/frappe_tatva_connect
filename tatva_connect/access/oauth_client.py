# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An OAuth client's redirect URIs are stored in the form frappe's own validator reads them in.

THE DEFECT. `OAuth Client.redirect_uris` is written newline-separated — the field's own description says
so, and frappe's dynamic registration joins with "\\n" (`integrations/utils.create_new_oauth_client`) —
but every read splits it on `frappe.oauth.get_url_delimiter()`, which is not a newline: `validate_redirect_uri`
and the code-for-token check both do `redirect_uris.split(get_url_delimiter())`. One URI survives that,
because splitting a single line changes nothing. Two or more never match again,
and the caller is told "Mismatching redirect URI" for a URI the record plainly holds.

A client registering ITSELF is where this bites: an agent that offers several callbacks (live, test and
sandbox variants of the same address) registers all of them and can then authorize with none. It is not
reachable by an operator either, because such a client is minted fresh on every connection attempt, so
editing the row by hand is outrun by the next one.

THE FIX IS THE FORMAT, NOT A SECOND MATCHER. The value is normalised to what the reader expects, so both
of frappe's own checks pass and nothing here decides whether a URI is allowed — that stays frappe's.
Bound as a class override rather than a doc_event: this is infrastructure, never an operator toggle.
"""
import frappe
from frappe.integrations.doctype.oauth_client.oauth_client import OAuthClient
from frappe.oauth import get_url_delimiter


class TatvaOAuthClient(OAuthClient):
	def validate(self):
		super().validate()
		self.redirect_uris = as_frappe_reads_them(self.redirect_uris)


def as_frappe_reads_them(value: str) -> str:
	"""The URIs joined on frappe's OWN delimiter — the one its readers split on. Blank stays blank."""
	return get_url_delimiter().join(frappe.utils.cstr(value).split())
