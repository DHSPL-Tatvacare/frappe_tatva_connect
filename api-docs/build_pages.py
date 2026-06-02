#!/usr/bin/env python3
"""Generate Zudoku MDX pages + navigation snippet from openapi.json narrative.
Flat pages/<slug>.mdx to keep routes simple. MDX-safe escaping of < > { } outside code."""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
spec = json.load(open(os.path.join(HERE, "openapi.json")))

def mdx_escape(md: str) -> str:
    out = []
    for part in re.split(r"(```.*?```)", md, flags=re.DOTALL):
        if part.startswith("```"):
            out.append(part); continue
        for seg in re.split(r"(`[^`]*`)", part):
            if seg.startswith("`") and seg.endswith("`"):
                out.append(seg)
            else:
                seg = seg.replace("{", "&#123;").replace("}", "&#125;")
                seg = seg.replace("<", "&lt;").replace(">", "&gt;")
                out.append(seg)
    return "".join(out)

def slug(name): return name.lower().replace(" ", "-").replace("&", "and")

def write_page(s, title, body):
    with open(os.path.join(HERE, "pages", f"{s}.mdx"), "w") as f:
        f.write(f"---\ntitle: {title}\n---\n\n" + mdx_escape(body).strip() + "\n")

# intro
write_page("introduction", spec["info"]["title"], spec["info"].get("description", ""))

tagdesc = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}
made = {}
for name, desc in tagdesc.items():
    if desc.strip():
        s = slug(name); write_page(s, name, desc); made[name] = s

# navigation from x-tagGroups
nav_categories = []
for i, g in enumerate(spec.get("x-tagGroups", [])):
    items = [made[t] for t in g["tags"] if t in made]
    if i == 0:
        items = ["introduction"] + items
    if items:
        nav_categories.append({"label": g["name"], "items": items})

# emit a JSON the TS config will import-inline
with open(os.path.join(HERE, "nav.json"), "w") as f:
    json.dump(nav_categories, f, indent=2)

print("pages:", sorted(os.listdir(os.path.join(HERE, "pages"))))
print("nav:", [(c["label"], c["items"]) for c in nav_categories])
