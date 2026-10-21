# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Partner API docs — the guides and the spec the public `/docs` site is built from, read from the app.

`api-docs/` ships with the app (`bench get-app` keeps the whole repo), so the server reads the SOURCE: the guide
pages (`pages/*.mdx`) and `openapi-partner.json`, the one file Zudoku builds `/docs` from. Nothing here can
drift from what a partner reads.

Each file is parsed once per worker and again only when it changes on disk: a call costs a `stat`, never a parse,
and never a database read. The guides join the handbook tools as one more space (`docs.py` routes to them); the
spec is two tools of its own, `search_api` and `get_api`.
"""
import functools
import json
import os
import re

import frappe
from frappe.utils import get_url

from tatva_connect.mcp import ToolError, settings

SPACE = "docs"  # the path `/docs` serves the guides under, so a page's route is its own address
SPACE_NAME = "Partner API"
METHODS = ("get", "post", "put", "patch", "delete")
# How deep a shared definition is filled in; past it the answer names the schema and stops.
REF_DEPTH = 3

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_MERMAID = re.compile(r"<Mermaid\s+chart=\{`(.*?)`\}\s*/>", re.S)
_BADGE = re.compile(r"<Badge[^>]*>(.*?)</Badge>", re.S)
_TAG = re.compile(r"</?[A-Z][A-Za-z]*[^>]*/?>")
_IMPORT = re.compile(r"^import .*$", re.M)
_WORD = re.compile(r"[a-z0-9]+")


def _source(*parts):
	# Joined by os.path: frappe's own path helpers scrub hyphens, and the folder is `api-docs`.
	return os.path.join(frappe.get_app_source_path("tatva_connect"), "api-docs", *parts)


# -- parsed once per file version -------------------------------------------
@functools.lru_cache(maxsize=2)
def _spec_at(path, _mtime):
	with open(path, encoding="utf-8") as spec:
		return json.load(spec)


@functools.lru_cache(maxsize=2)
def _pages_at(folder, _versions):
	pages = {}
	for name in sorted(os.listdir(folder)):
		if name.endswith(".mdx"):
			with open(os.path.join(folder, name), encoding="utf-8") as page:
				pages[name.removesuffix(".mdx")] = _page(page.read())
	return pages


def _version():
	path = _source("openapi-partner.json")
	return path, os.stat(path).st_mtime


def _spec():
	return _spec_at(*_version())


def _pages():
	folder = _source("pages")
	versions = tuple((name, os.stat(os.path.join(folder, name)).st_mtime) for name in sorted(os.listdir(folder)))
	return _pages_at(folder, versions)


def _page(raw):
	"""One guide as plain markdown: its frontmatter title, and the body with Zudoku's components unwrapped."""
	front = _FRONT.match(raw)
	meta = dict(re.findall(r"^(\w+):\s*\"?(.*?)\"?$", front.group(1), re.M)) if front else {}
	body = raw[front.end():] if front else raw
	body = _MERMAID.sub(lambda m: f"```mermaid\n{m.group(1).strip()}\n```", body)
	body = _TAG.sub("", _BADGE.sub(r"\1", _IMPORT.sub("", body)))
	title, description, text = meta.get("title") or "", meta.get("description") or "", body.strip()
	return {"title": title, "description": description, "text": text, "index": _index(title, description, text)}


def _index(title, *rest):
	"""The word sets a search scores against, built once when the file is read."""
	return _bag(title), _bag(" ".join(rest))


def _bag(text):
	return frozenset(_WORD.findall((text or "").lower()))


def _score(words, index):
	"""How well an entry answers the words: a word in the title counts three times, anywhere else once."""
	title, rest = index
	return sum(3 * (word in title) + (word in rest) for word in words)


def _words(query):
	return [word for word in _WORD.findall((query or "").lower()) if len(word) > 1]


# -- the guide space, as docs.py asks for it ----------------------------------
def space():
	return {"space": SPACE_NAME, "route": SPACE, "url": get_url(f"/{SPACE}")}


def contents():
	return [{"title": page["title"], "route": f"{SPACE}/{slug}", "url": get_url(f"/{SPACE}/{slug}"), "section": False}
	        for slug, page in _pages().items()]


def page(route):
	"""One guide by its route (`docs/<page>`), or None when the route is not a guide."""
	slug = route.removeprefix(f"{SPACE}/")
	found = _pages().get(slug) if route.startswith(f"{SPACE}/") else None
	return {**found, "route": route, "url": get_url(f"/{route}")} if found else None


def search(query, limit):
	words = _words(query)
	ranked = sorted(((_score(words, p["index"]), slug, p) for slug, p in _pages().items()),
	                key=lambda hit: -hit[0])
	return [{"title": p["title"], "route": f"{SPACE}/{slug}", "url": get_url(f"/{SPACE}/{slug}"),
	         "snippet": p["description"], "score": score} for score, slug, p in ranked[:limit] if score]


# -- the spec -------------------------------------------------------------------
def _anchor(text):
	"""Zudoku's heading id: lower case, punctuation dropped, spaces to hyphens."""
	return re.sub(r"\s+", "-", re.sub(r"[^a-z0-9\s-]", "", text.lower())).strip("-")


@functools.lru_cache(maxsize=2)
def _operations_at(path, mtime):
	spec = _spec_at(path, mtime)
	operations = []
	for path, item in spec["paths"].items():
		for method, op in item.items():
			if method not in METHODS:
				continue
			summary, tag = op.get("summary") or "", (op.get("tags") or [""])[0]
			operations.append({"operation": op.get("operationId"), "method": method.upper(), "path": path,
			                   "summary": summary, "tag": tag, "description": op.get("description") or "", "spec": op,
			                   "index": _index(f"{summary} {op.get('operationId')} {tag}", path, op.get("description"))})
	return operations


def _operations():
	return _operations_at(*_version())


def _url(op):
	return get_url(f"/{SPACE}/partner-api/{_anchor(op['tag'])}#{_anchor(op['summary'])}")


def _resolve(node, depth=0):
	"""The spec with `$ref`s filled in to REF_DEPTH; deeper ones are named, so the answer stays bounded."""
	if isinstance(node, list):
		return [_resolve(item, depth) for item in node]
	if not isinstance(node, dict):
		return node
	if "$ref" in node:
		name = node["$ref"].rsplit("/", 1)[-1]
		if depth >= REF_DEPTH:
			return {"schema": name}
		target = _spec()
		for part in node["$ref"].lstrip("#/").split("/"):
			target = target[part]
		return _resolve(target, depth + 1)
	return {key: _resolve(value, depth) for key, value in node.items() if key != "examples"}


# -- tools ------------------------------------------------------------------------
def search_api(arguments):
	"""Endpoints that match some words: method, path, summary and the /docs address of each."""
	words = _words(arguments.get("query"))
	if not words:
		raise ToolError("Give a word or phrase to search for, such as 'create lead' or 'ticket comments'.")
	cap = settings.config()["search_max_hits"]
	ranked = sorted(((_score(words, op["index"]), op) for op in _operations()), key=lambda hit: -hit[0])
	hits = [op for score, op in ranked if score]
	return {
		"query": arguments.get("query"),
		"endpoints": [{"operation": op["operation"], "method": op["method"], "path": op["path"],
		               "summary": op["summary"], "url": _url(op)} for op in hits[:cap]],
		"more": len(hits) > cap,
		"next": "Call get_api on an operation for its parameters, body and responses."
		        if hits else "Nothing matches. Try the resource's name, such as lead, call or ticket.",
	}


def get_api(arguments):
	"""One endpoint in full: parameters, request body, responses and its /docs address."""
	wanted = (arguments.get("operation") or "").strip()
	op = next((op for op in _operations() if op["operation"] == wanted), None)
	if not op:
		raise ToolError(f"No endpoint '{wanted}'. search_api finds an operation by words.")
	spec = _resolve(op["spec"])
	responses = spec.get("responses") or {}
	failures = {code: body for code, body in responses.items() if code[0] in "45"}
	# Every error shares one envelope: each code keeps its description, and the body is shown once.
	shared = next((body.get("content") for body in failures.values() if body.get("content")), None)
	return {
		"operation": op["operation"], "method": op["method"], "path": op["path"], "summary": op["summary"],
		"url": _url(op), "description": op["description"],
		"parameters": spec.get("parameters") or [],
		"request_body": (spec.get("requestBody") or {}).get("content"),
		"responses": {code: body for code, body in responses.items() if code not in failures},
		"errors": {code: body.get("description") for code, body in failures.items()},
		"error_body": shared,
	}
