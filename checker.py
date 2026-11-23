import json, re, sys, time, threading, urllib.request, html, datetime, pathlib, http.server, functools
import yaml
import urllib.parse
from zoneinfo import ZoneInfo

CFG = pathlib.Path("software.yml")
DATA = pathlib.Path("docs")

def vkey(v): return tuple(int(x) for x in v.split("."))

@functools.lru_cache(maxsize=None)
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 update-watcher"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")

def debug_dump(name, page):
    print(f"--- DEBUG {name}: {len(page)} tekens ontvangen", flush=True)
    show = lambda tag, items, n: [print("    ", tag, " ".join(m.split())[:200], flush=True) for m in items[:n]]
    show("INPUT", re.findall(r"<input[^>]*(?:version|release)[^>]*>", page, re.I), 20)
    show("DATA ", re.findall(r"data-[\w-]*(?:version|release)[\w-]*=\"[^\"]*\"", page, re.I), 10)
    show("LINK ", re.findall(r"href=\"[^\"]*\.(?:exe|dmg|zip|msi|pkg)[^\"]*\"", page, re.I), 10)
    show("TEKST", re.findall(r">[^<>]{0,60}(?:[Vv]ersion|[Rr]elease)[^<>]{0,60}<", page), 12)

def check(it):
    if "version" in it:  # handmatig bijgehouden versie
        if not str(it["version"]).strip():
            raise ValueError("versie nog niet ingevuld in software.yml")
        return str(it["version"]).strip()
    if it.get("github"):
        d = json.loads(fetch(f"https://api.github.com/repos/{it['github']}/releases/latest"))
        return d["tag_name"].lstrip("v")
    if it.get("json"):
        d = json.loads(fetch(it["json"]))
        for k in it["key"].split("."):
            d = d[k]
        return re.search(r"\d+(?:\.\d+)+", str(d)).group(0)
    page = fetch(it["url"])
    found = re.findall(it["regex"], page)
    if not found:
        debug_dump(it["name"], page)
        raise ValueError("geen versie gevonden (regex klopt niet meer?)")
    return max(found, key=vkey)  # hoogste versie op de pagina

def resolve_downloads(it, ver):
    out = []
    for d in it.get("downloads", []):
        try:
            if "url" in d:  # vaste link met {version}
                out.append({"label": d["label"], "url": d["url"].replace("{version}", ver)})
            else:           # link uit de pagina halen met een regex (één groep = de href)
                m = re.search(d["regex"].replace("{version}", re.escape(ver)), fetch(it["url"]))
                if m:
                    out.append({"label": d["label"], "url": urllib.parse.urljoin(it["url"], html.unescape(m.group(1)))})
        except Exception:
            pass
    return out

def run():
    cfg = yaml.safe_load(CFG.read_text())
    f = DATA / "versions.json"
    old = {i["name"]: i for i in json.loads(f.read_text())} if f.exists() else {}
    now = datetime.datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M")
    out = []
    for it in cfg["items"]:
        o = old.get(it["name"], {})
        r = {"name": it["name"], "category": it.get("category", "Overig"),
             "link": it.get("link") or it.get("url"), "note": it.get("note", ""),
             "version": o.get("version"), "previous": o.get("previous"),
             "changed": o.get("changed"), "error": None, "checked": now}
        try:
            v = check(it)
            if v != r["version"]:
                r["previous"], r["version"], r["changed"] = r["version"], v, now
        except Exception as e:
            r["error"] = str(e)[:200]
        print(("OK   " if not r["error"] else "FOUT ") + it["name"] + ": " + (r["error"] or r["version"]), flush=True)
        r["downloads"] = resolve_downloads(it, r["version"]) if r["version"] else []
        out.append(r)
    f.write_text(json.dumps(out, indent=1))
    (DATA / "index.html").write_text(render(out, now))
    return cfg.get("interval_hours", 6)

def render(items, now):
    e = html.escape
    cats = {}
    for i in items: cats.setdefault(i["category"], []).append(i)
    body = ""
    for c in sorted(cats):
        body += f"<h2>{e(c)}</h2><table>"
        for i in cats[c]:
            ver = e(i["version"] or "onbekend")
            ch = f"gewijzigd {e(i['changed'])}" if i["changed"] and i["previous"] else ""
            warn = f"<span class=w>⚠ {e(i['error'])}</span>" if i["error"] else ""
            note = f"<small>{e(i['note'])}</small>" if i["note"] else ""
            dl = (" · ".join(f"<a href='{e(d['url'])}'>{e(d['label'])}</a>" for d in i["downloads"])
                  if i.get("downloads") else f"<a href='{e(i['link'])}'>Download</a>")
            body += (f"<tr><td><b>{e(i['name'])}</b>{note}</td><td>{ver}</td><td>{ch}{warn}</td>"
                     f"<td>{dl}</td></tr>")
        body += "</table>"
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Software updates</title><style>
:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}body{{max-width:900px;margin:2rem auto;padding:0 1rem}}
table{{width:100%;border-collapse:collapse}}td{{padding:.5rem;border-bottom:1px solid #8884}}small{{display:block;opacity:.6}}
.w{{color:#c60}}a{{text-decoration:none;font-weight:600}}</style>
<h1>Software updates</h1><p>Laatst gecontroleerd: {e(now)}</p>{body}"""

if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    run()
