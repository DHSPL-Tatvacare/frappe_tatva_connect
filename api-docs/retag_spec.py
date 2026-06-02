#!/usr/bin/env python3
"""Transform the OpenAPI spec from HTTP-action tags -> resource-oriented tags (LSQ-style).
Reads the untouched vault original, writes the resource-grouped spec into the Zudoku project."""
import json, os

VAULT = "/Users/dhspl/Programs/tc-work/tatvacare-obsidian/Projects/frappe-crm/Frappe Migration 101/scripts/openapi.json"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openapi.json")

spec = json.load(open(VAULT))

# old action-tag -> new resource-tag
REMAP = {
    "Write": "Leads",
    "Read": "Leads",
    "Attachments": "Files",
    "Lookups": "Reference Data",
    "Discovery": "Schema",
    "Authentication": "Authentication",
}

# 1) re-tag every operation
for p, methods in spec.get("paths", {}).items():
    for m, op in methods.items():
        if not isinstance(op, dict):
            continue
        new = []
        for t in op.get("tags", []):
            nt = REMAP.get(t)
            if nt and nt not in new:
                new.append(nt)
        if new:
            op["tags"] = new

# 2) original tag descriptions (for folding)
od = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}

def join(*names):
    return "\n\n---\n\n".join(od[n].strip() for n in names if od.get(n, "").strip())

# 3) rebuild resource tags with folded narrative
spec["tags"] = [
    {"name": "Authentication", "description": od.get("Authentication", "")},
    {"name": "Leads",          "description": join("Write", "Read")},
    {"name": "Files",          "description": od.get("Attachments", "")},
    {"name": "Reference Data", "description": od.get("Lookups", "")},
    {"name": "Schema",         "description": od.get("Discovery", "")},
]

# 4) resource-oriented top-level grouping
spec["x-tagGroups"] = [
    {"name": "Getting started", "tags": ["Authentication"]},
    {"name": "Resources",       "tags": ["Leads", "Files"]},
    {"name": "Reference",       "tags": ["Reference Data", "Schema"]},
]

json.dump(spec, open(OUT, "w"), indent=2)

# report
from collections import Counter
c = Counter()
for p, methods in spec["paths"].items():
    for m, op in methods.items():
        if isinstance(op, dict):
            for t in op.get("tags", []):
                c[t] += 1
print("ops per new tag:", dict(c))
print("tag groups:", [(g["name"], g["tags"]) for g in spec["x-tagGroups"]])
