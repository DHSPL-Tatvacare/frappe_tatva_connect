# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Read the partner API's OpenAPI spec. Used by the drift lock in test_openapi_matches_reality."""
import json
import os

import frappe

_SPEC = "api-docs/openapi-partner.json"


def spec_file():
	"""The spec's absolute path, from the app root (the bench app dir, not the site)."""
	app = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../tatva_connect
	return os.path.join(os.path.dirname(app), _SPEC)


def load_spec():
	path = spec_file()
	if not os.path.exists(path):
		frappe.throw(f"the partner OpenAPI spec is missing: {path}")
	with open(path) as fh:
		return json.load(fh)


def spec_paths(spec):
	return list(spec.get("paths", {}))


def _deref(spec, node):
	"""Resolve a local $ref one hop."""
	if isinstance(node, dict) and "$ref" in node:
		parts = node["$ref"].lstrip("#/").split("/")
		out = spec
		for p in parts:
			out = out[p]
		return out
	return node


def response_example(spec, path, status="200"):
	"""The first documented example for a path's response, or None."""
	node = spec.get("paths", {}).get(path)
	if not node:
		return None
	for verb, op in node.items():
		if verb.lower() not in ("get", "post", "put", "delete", "patch"):
			continue
		content = (op.get("responses", {}).get(status, {})
		           .get("content", {}).get("application/json", {}))
		examples = content.get("examples")
		if not examples:
			return None
		first = _deref(spec, next(iter(examples.values())))
		return first.get("value") if isinstance(first, dict) else None
	return None
