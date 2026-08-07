# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The control tower as a VIRTUAL doctype, in the shape of frappe's own `System Health Report`.

Virtual is what makes "read only" structural rather than a promise. There is no table behind this, so
there is nothing to write to; `db_insert`, `db_update` and `delete` raise, so the document layer cannot
write either, and the role gate is an ordinary DocPerm row (System Manager, read) rather than a check
somebody has to remember to make. Desk renders it at /app/crm-control-tower with no extra wiring.

The collection itself lives in `observability/control_tower.py` and is shared with the whitelisted
method, so the page and the endpoint can never disagree.
"""

import json

import frappe
from frappe.model.document import Document

from tatva_connect.observability import control_tower


class CRMControlTower(Document):
	def load_from_db(self):
		super(Document, self).__init__({})
		frappe.only_for("System Manager")

		payload = control_tower.collect()
		self.verdict = payload["verdict"]
		self.problems = "\n".join(payload["problems"]) or "none"
		self.collected = f"{payload['meta']['site']} · {payload['meta']['collected_at']} · {payload['meta']['elapsed_ms']} ms"
		self.config = json.dumps(payload["config"], indent=2, default=str)
		self.platform = json.dumps(payload["platform"], indent=2, default=str)
		self.tatva = json.dumps(payload["tatva"], indent=2, default=str)

	def db_insert(self, *args, **kwargs):
		raise NotImplementedError

	def db_update(self, *args, **kwargs):
		raise NotImplementedError

	def delete(self, *args, **kwargs):
		raise NotImplementedError

	@staticmethod
	def get_list(*args, **kwargs):
		raise NotImplementedError

	@staticmethod
	def get_count(*args, **kwargs):
		raise NotImplementedError

	@staticmethod
	def get_stats(*args, **kwargs):
		raise NotImplementedError
