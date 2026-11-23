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

def page_version(it):
    page = fetch(it["url"])
    found = re.findall(it["regex"], page, re.S)
    if not found:
        debug_dump(it["name"], page)
        raise ValueError("geen versie gevonden (regex klopt niet meer?)")
    found = [".".join(f) if isinstance(f, tuple) else f for f in found]   # meerdere groepen: samenvoegen met punt (bijv. 4 en 13 wordt 4.13)
    if it.get("replace"):                         # bijv. ["_", "."] om 8_1_7 om te zetten naar 8.1.7
        found = [f.replace(it["replace"][0], it["replace"][1]) for f in found]
    vs = sorted(set(found), key=vkey, reverse=True)  # hoogste eerst; nummer 2 = vorige versie
    return vs[0], (vs[1] if len(vs) > 1 and it.get("previous_from_page", True) else None)

def check(it):
    """Geeft (huidige versie, vorige versie volgens de pagina of None)."""
    if "version" in it:  # handmatig bijgehouden versie
        if not str(it["version"]).strip():
            raise ValueError("versie nog niet ingevuld in software.yml")
        return str(it["version"]).strip(), None
    if it.get("github"):
        d = json.loads(fetch(f"https://api.github.com/repos/{it['github']}/releases/latest"))
        return d["tag_name"].lstrip("v"), None
    if it.get("url") and it.get("regex"):
        try:
            return page_version(it)
        except Exception as ex:
            if not it.get("json"):
                raise
            print(f"INFO {it['name']}: pagina mislukt ({ex}), reservebron gebruikt", flush=True)
    d = json.loads(fetch(it["json"]))
    for k in it["key"].split("."):
        d = d[k]
    return re.search(r"\d+(?:\.\d+)+", str(d)).group(0), None

# ---- release-datum, wijzigingen en link naar release notes (allemaal optioneel) ----
def fill(t, ver):
    return t.replace("{version_dash}", ver.replace(".", "-")).replace("{version_us}", ver.replace(".", "_")).replace("{version}", ver)

def parse_date(s):
    s = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", s.strip().replace(" of ", " "))
    for f in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %d %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None

LABELS = {"fixed", "fix", "new", "improved", "improvement", "changed", "added", "removed", "feature"}

def strip_html(h):
    h = re.sub(r"</?(?:li|p|div|br|span|h\d|tr|td|section|article)\b[^>]*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    lines = [html.unescape(" ".join(l.split())) for l in h.split("\n")]
    lines = [l for l in lines if l]
    out = []
    for l in lines:
        if out and out[-1].lower().rstrip(":") in LABELS:
            out[-1] = f"{out[-1].rstrip(':')}: {l}"
        else:
            out.append(l)
    return out[:30]

def hint(name, what, page, ver, needle=None):
    k = page.find(ver)
    around = " ".join(page[max(0, k - 80):k + 300].split()) if k >= 0 else f"versie {ver} komt niet voor in de pagina"
    print(f"INFO {name}: {what} niet gevonden. Pagina rond {ver}: {around}", flush=True)
    if needle and page.find(needle) >= 0:
        n = page.find(needle)
        print(f"INFO {name}: pagina vanaf {needle}: " + " ".join(page[n:n + 1100].split()), flush=True)

def extras(it, r):
    ver, name = r["version"], r["name"]
    released = None
    if str(it.get("released", "")).strip():                       # handmatig ingevuld
        released = parse_date(str(it["released"])) or str(it["released"]).strip()
    elif it.get("date_regex"):
        try:
            page = fetch(fill(it.get("date_url", it.get("url", "")), ver))
            m = re.search(it["date_regex"].replace("{version}", re.escape(ver)), page, re.S)
            released = parse_date(next((g for g in m.groups() if g), "")) if m else None
            if not released:
                hint(name, "releasedatum", page, ver)
        except Exception as ex:
            print(f"INFO {name}: releasedatum ophalen mislukt: {ex}", flush=True)
    r["released"] = released
    notes = None
    if it.get("notes_regex"):
        try:
            page = fetch(fill(it.get("notes_page", it.get("url", "")), ver))
            m = re.search(it["notes_regex"].replace("{version}", re.escape(ver)), page, re.S)
            notes = strip_html(next((g for g in m.groups() if g), "")) if m else None
            if not notes:
                hint(name, "wijzigingen", page, ver, needle="<h1")
        except Exception as ex:
            print(f"INFO {name}: wijzigingen ophalen mislukt: {ex}", flush=True)
    r["notes"] = notes or None
    r["notes_url"] = fill(it["notes_url"], ver) if it.get("notes_url") else None
    r["notes_label"] = it.get("notes_label", "Release notes")

def resolve_downloads(it, ver):
    out = []
    for d in it.get("downloads", []):
        try:
            if "url" in d:  # vaste link met {version}
                out.append({"label": d["label"], "url": d["url"].replace("{version}", ver)})
            else:           # link uit de pagina halen met een regex (één groep = de href)
                m = re.search(d["regex"].replace("{version_us}", ver.replace(".", "_")).replace("{version}", re.escape(ver)), fetch(it["url"]))
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
        r = {"name": it["name"], "category": it.get("category", ""),
             "link": it.get("link") or it.get("url"), "note": it.get("note", ""),
             "version": o.get("version"), "previous": o.get("previous"),
             "changed": o.get("changed"), "error": None, "checked": now}
        try:
            v, prev_page = check(it)
            if v != r["version"]:
                r["previous"], r["version"], r["changed"] = r["version"], v, now
            if prev_page:
                r["previous"] = prev_page   # echte vorige versie uit de pagina gaat voor
        except Exception as e:
            r["error"] = str(e)[:200]
        print(("OK   " if not r["error"] else "FOUT ") + it["name"] + ": " + (r["error"] or r["version"]), flush=True)
        r["downloads"] = resolve_downloads(it, r["version"]) if r["version"] else []
        r["released"], r["notes"], r["notes_url"] = None, None, None
        if r["version"] and not r["error"]:
            extras(it, r)
        out.append(r)
    f.write_text(json.dumps(out, indent=1))
    (DATA / "index.html").write_text(render(out, now, cfg.get("contact"), cfg.get("categories")))
    return cfg.get("interval_hours", 6)

PALETTE = ["#c23f48", "#8ea2d8", "#ebe8e2", "#ff6b78", "#6f8bff", "#9ea3b3"]  # kleur per categorie, in volgorde van software.yml
MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
NEW_DAYS = 14
FMT = "%Y-%m-%d %H:%M"

def nl_date(s):
    try:
        d = datetime.datetime.strptime(s, FMT)
        return f"{d.day} {MONTHS[d.month - 1]}"
    except Exception:
        return ""

def nl_iso(s):
    try:
        d = datetime.datetime.strptime(s, "%Y-%m-%d")
        return f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    except Exception:
        return s

def nl_datetime(s):
    try:
        return f"{nl_date(s)}, {datetime.datetime.strptime(s, FMT):%H:%M}"
    except Exception:
        return s

def is_new(i, now):
    try:
        if not i["previous"] or not i["changed"]:
            return False
        return (datetime.datetime.strptime(now, FMT) - datetime.datetime.strptime(i["changed"], FMT)).days < NEW_DAYS
    except Exception:
        return False

ICON = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v8m0 0L4.5 6.5M8 10l3.5-3.5M3 13.5h10"/></svg>'

def row_html(i, now, cat=""):
    e = html.escape
    new = is_new(i, now)
    badge = '<span class="badge">Nieuw</span>' if new else ""
    note = f'<span class="note">{e(i["note"])}</span>' if i["note"] else ""
    was = ""
    if i["previous"]:
        was += f'<span class="was">Vorige versie {e(i["previous"])}</span>'
    if i.get("released"):
        was += f'<span class="was">Uitgebracht {e(nl_iso(i["released"]))}</span>'
    elif i["previous"] and i["changed"]:
        was += f'<span class="was">Ontdekt op {e(nl_date(i["changed"]))}</span>'
    err = f'<span class="err">Controle mislukt: {e(i["error"])}</span>' if i["error"] else ""
    if i.get("downloads"):
        dl = "".join(f'<a class="btn" href="{e(d["url"])}">{ICON}{e(d["label"])}</a>' for d in i["downloads"])
    else:
        dl = f'<a class="btn" href="{e(i["link"] or "#")}">{ICON}Download</a>'
    more = ""
    if i.get("notes"):
        more += '<details><summary>Wijzigingen</summary><ul>' + "".join(f"<li>{e(l)}</li>" for l in i["notes"]) + "</ul></details>"
    if i.get("notes_url"):
        more += f'<a href="{e(i["notes_url"])}" target="_blank" rel="noopener">{e(i["notes_label"])}</a>'
    more = f'<div class="more">{more}</div>' if more else ""
    return (f'<li class="row{" new" if new else ""}" data-cat="{e(cat)}"><div class="who"><span class="name">{e(i["name"])}</span>{badge}{note}</div>'
            f'<div class="ver"><span class="num">{e(i["version"] or "onbekend")}</span>{was}{err}</div>'
            f'<div class="dl">{dl}</div>{more}</li>')

def render(items, now, contact=None, categories=None):
    e = html.escape
    # categorieën uit software.yml (volgorde = volgorde van de zijbalk); een categorie die alleen bij een programma staat komt erbij
    canon = {}
    for c in (categories or []):
        if str(c).strip():
            canon.setdefault(str(c).strip().lower(), str(c).strip())
    for i in items:
        c = str(i.get("category") or "").strip()
        if c:
            canon.setdefault(c.lower(), c)
    cat_of = lambda i: canon.get(str(i.get("category") or "").strip().lower(), "")
    n_new = sum(is_new(i, now) for i in items)
    n_err = sum(bool(i["error"]) for i in items)
    if n_new:
        summary = f"{n_new} programma heeft een nieuwe versie" if n_new == 1 else f"{n_new} programma's hebben een nieuwe versie"
    else:
        summary = f"Geen nieuwe versies in de afgelopen {NEW_DAYS} dagen"
    if n_err:
        summary += f", bij {n_err} lukte de controle niet"
    summary += "."

    rows = "".join(row_html(i, now, cat_of(i)) for i in items)
    listing = f'<ul class="list">{rows}</ul><p class="empty" id="empty" hidden></p>'
    if canon:
        nav = f'<button type="button" data-cat="*" aria-current="true"><span>ALL</span><b>{len(items)}</b></button>'
        for c in canon.values():
            nav += f'<button type="button" data-cat="{e(c)}" aria-current="false"><span>{e(c)}</span><b>{sum(cat_of(i) == c for i in items)}</b></button>'
        layout = f'<div class="layout"><nav class="cats" aria-label="Categorieën">{nav}</nav><div>{listing}</div></div>'
    else:
        layout = listing

    contact = contact or {}
    endpoint, mail, key = (str(contact.get(k) or "").strip() for k in ("endpoint", "email", "access_key"))
    ask = dialog = ""
    if endpoint:
        ask = '<button class="ask" id="ask" type="button">Mis je iets?</button>'
        keyfield = f'<input type="hidden" name="access_key" value="{e(key)}">' if key else ""
        dialog = DIALOG.replace("__ENDPOINT__", e(endpoint)).replace("__KEYFIELD__", keyfield)
    elif mail:
        ask = f'<a class="ask" href="mailto:{e(mail)}?subject=Software%20updates">Mis je iets?</a>'
    return (PAGE.replace("__ASK__", ask).replace("__DIALOG__", dialog).replace("__NOW__", e(nl_datetime(now)))
                .replace("__SUMMARY__", e(summary)).replace("__LAYOUT__", layout))

PAGE = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Software Update Watcher</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg viewBox='0 0 48 48' xmlns='http://www.w3.org/2000/svg'%3E%3Crect width='48' height='48' rx='11' fill='%2326304f'/%3E%3Ccircle cx='24' cy='25' r='14' fill='%230e1118' stroke='%238ea2d8' stroke-width='1.5'/%3E%3Cg stroke='%23ebe8e2' stroke-width='1.6' stroke-linecap='round' opacity='.55'%3E%3Cpath d='M10.5 31l-2.2 1.3M7.8 24H10M10.5 17L8.3 15.7M37.5 31l2.2 1.3M40.2 24H38M37.5 17l2.2-1.3'/%3E%3C/g%3E%3Cpath d='M24 25L33 14.5' stroke='%23c23f48' stroke-width='3.2' stroke-linecap='round'/%3E%3Ccircle cx='24' cy='25' r='3.2' fill='%23ebe8e2'/%3E%3Cpath d='M33 8.5l4.5 0 0 4.5' fill='none' stroke='%23c23f48' stroke-width='2' stroke-linecap='round' stroke-linejoin='round' transform='translate(-1 0)'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@700;800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;--display:"Poppins","Arial Black","Helvetica Neue",Arial,sans-serif;
--bg:#0e1118;--surface:#151a26;--raise:#1b2132;--line:#252d41;--text:#ebe8e2;--muted:#8d95ab;
--blue:#8ea2d8;--blue-deep:#26304f;--red:#c23f48;--red-deep:#43191f;--red-soft:#d9636b}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;min-height:100vh;color:var(--text);font:14px/1.5 "Instrument Sans",system-ui,sans-serif;
background:radial-gradient(60rem 22rem at 12% -6rem,color-mix(in srgb,var(--blue-deep) 70%,transparent),transparent 70%),radial-gradient(40rem 18rem at 100% -4rem,color-mix(in srgb,var(--red-deep) 55%,transparent),transparent 70%),var(--bg)}
.wrap{max-width:1080px;margin:0 auto;padding:clamp(1.25rem,4vw,2.75rem) clamp(1rem,3vw,2rem) 3rem}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:.75rem 1.5rem;padding-bottom:1.1rem;border-bottom:1px solid transparent;border-image:linear-gradient(90deg,var(--blue),var(--red) 55%,transparent) 1}
.brand{display:flex;flex-direction:column;gap:.55rem}
.by{margin:0;color:var(--blue);font-size:.78rem;font-weight:500;letter-spacing:.02em}
h1{display:flex;align-items:center;gap:.75rem;font:800 clamp(1.15rem,3.6vw,1.65rem)/1 var(--display);letter-spacing:0;text-transform:uppercase;margin:0}
h1 em{font-style:normal;color:var(--red)}
.logo{width:2.2em;height:2.2em;flex:none}
.checked{color:var(--muted);font-size:.78rem;margin:0}
.summary{color:var(--muted);font-size:.9rem;margin:1.25rem 0 1.75rem}
.layout{display:grid;grid-template-columns:11rem minmax(0,1fr);gap:2.5rem;align-items:start}
.cats{position:sticky;top:1.5rem;display:flex;flex-direction:column;gap:.1rem;padding-left:.25rem;border-left:1px solid var(--line)}
.cats button{position:relative;display:flex;justify-content:space-between;align-items:center;gap:.5rem;width:100%;padding:.5rem .75rem;border:0;border-radius:8px;background:none;color:var(--muted);font:600 .8rem/1.2 "Instrument Sans",sans-serif;text-align:left;cursor:pointer;transition:color .15s,background .15s}
.cats button b{font-weight:500;font-size:.72rem;color:var(--muted)}
.cats button:hover{color:var(--text);background:color-mix(in srgb,var(--raise) 60%,transparent)}
.cats button[aria-current=true]{background:var(--raise);color:var(--text)}
.cats button[aria-current=true]::before{content:"";position:absolute;left:-.5rem;top:25%;bottom:25%;width:2px;border-radius:2px;background:var(--red)}
.cats button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.list{list-style:none;margin:0;padding:0}
.row{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 11rem 12.5rem;gap:.4rem 1.5rem;align-items:center;padding:.95rem .25rem;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row.new::before{content:"";position:absolute;inset:.3rem -.75rem;z-index:-1;border-radius:12px;background:linear-gradient(90deg,color-mix(in srgb,var(--red-deep) 70%,transparent),transparent 80%)}
.name{font-weight:600;font-size:.95rem}
.note{display:block;color:var(--muted);font-size:.76rem;margin-top:.1rem}
.badge{display:inline-flex;align-items:center;gap:.4rem;margin-left:.6rem;padding:.08rem .55rem;border-radius:999px;background:var(--red-deep);color:var(--red-soft);font-size:.68rem;font-weight:600;vertical-align:.12em}
.badge::before{content:"";width:.38rem;height:.38rem;border-radius:50%;background:var(--red);animation:lamp 1.8s ease-in-out infinite}
@keyframes lamp{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.badge::before{animation:none}}
.num{display:inline-block;color:var(--blue);font:700 1.05rem/1.2 var(--display);letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.was{display:block;color:var(--muted);font-size:.74rem;margin-top:.05rem}
.err{display:block;color:var(--red-soft);font-size:.76rem;margin-top:.1rem}
.dl{display:flex;flex-wrap:wrap;gap:.4rem;justify-content:flex-end}
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.38rem .85rem;border:1px solid color-mix(in srgb,var(--blue) 45%,transparent);border-radius:999px;background:none;color:var(--text);font-size:.78rem;font-weight:600;text-decoration:none;transition:background .15s,color .15s,border-color .15s}
.btn:hover{background:var(--blue);border-color:var(--blue);color:#0e1118}
.btn:focus-visible,.more summary:focus-visible,.more a:focus-visible,.ask:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.btn svg{width:.95em;height:.95em}
.more{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:flex-start;gap:.4rem 1.5rem;font-size:.78rem}
.more summary{cursor:pointer;color:var(--blue);font-weight:600}
.more ul{margin:.4rem 0 0;padding-left:1rem;color:var(--muted);max-width:70ch}
.more a{color:var(--blue)}
.side{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem 1rem}
.ask{padding:.35rem .9rem;border:1px solid var(--red);border-radius:999px;background:none;color:var(--text);font:inherit;font-size:.78rem;font-weight:600;text-decoration:none;cursor:pointer}
.ask:hover{background:var(--red-deep)}
dialog{width:min(30rem,calc(100vw - 2rem));padding:1.25rem;border:1px solid var(--line);border-radius:14px;background:var(--surface);color:var(--text)}
dialog::backdrop{background:rgba(5,7,12,.75)}
dialog p{margin:.25rem 0 1rem;color:var(--muted);font-size:.85rem}
dialog label{display:block;margin:0 0 .8rem;color:var(--muted);font-size:.8rem}
dialog textarea,dialog input:not([type=hidden]):not([type=checkbox]){display:block;width:100%;margin-top:.25rem;padding:.5rem .6rem;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit;font-size:.9rem}
dialog textarea{resize:vertical}
dialog textarea:focus-visible,dialog input:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
#cstatus{min-height:1.3em;margin:.25rem 0 .75rem}
.actions{display:flex;gap:.5rem}
button.btn{font-family:inherit;cursor:pointer}
button.btn:disabled{opacity:.6;cursor:default}
.btn.ghost{color:var(--muted);border-color:var(--line)}
.btn.ghost:hover{background:var(--line);color:var(--text)}
[hidden]{display:none!important}
.empty{margin:0;padding:1.25rem;border:1px dashed var(--line);border-radius:12px;color:var(--muted)}
footer{margin-top:2rem;color:var(--muted);font-size:.75rem}
@media (max-width:760px){.layout{grid-template-columns:1fr;gap:1.25rem}
.cats{position:static;flex-direction:row;overflow-x:auto;gap:.4rem;padding:0 0 .25rem;border:0}
.cats button{width:auto;white-space:nowrap;gap:.45rem;border:1px solid var(--line);border-radius:999px}
.cats button[aria-current=true]{border-color:var(--red)}.cats button[aria-current=true]::before{display:none}
.row{grid-template-columns:1fr}.dl{justify-content:flex-start}}
</style></head><body><div class="wrap">
<header><div class="brand"><p class="by">by evertfront</p><h1><svg class="logo" viewBox="0 0 48 48" aria-hidden="true"><rect width="48" height="48" rx="11" fill="#26304f"/><circle cx="24" cy="25" r="14" fill="#0e1118" stroke="#8ea2d8" stroke-width="1.5"/><g stroke="#ebe8e2" stroke-width="1.6" stroke-linecap="round" opacity=".55"><path d="M10.5 31l-2.2 1.3M7.8 24H10M10.5 17L8.3 15.7M37.5 31l2.2 1.3M40.2 24H38M37.5 17l2.2-1.3"/></g><path d="M24 25L33 14.5" stroke="#c23f48" stroke-width="3.2" stroke-linecap="round"/><circle cx="24" cy="25" r="3.2" fill="#ebe8e2"/><path d="M33 8.5l4.5 0 0 4.5" fill="none" stroke="#c23f48" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" transform="translate(-1 0)"/></svg><span>Software <em>Update</em> Watcher</span></h1></div><div class="side"><p class="checked">Laatst gecontroleerd __NOW__</p>__ASK__</div></header>
<p class="summary">__SUMMARY__</p>
__LAYOUT__
<footer>De lijst wordt elke 6 uur automatisch bijgewerkt.</footer>
</div>
__DIALOG__
<script>
const btns=[...document.querySelectorAll('.cats button')],rows=[...document.querySelectorAll('.row')],list=document.querySelector('.list'),empty=document.getElementById('empty');
function show(cat){
  let n=0;
  rows.forEach(r=>{const on=cat==='*'||r.dataset.cat===cat;r.hidden=!on;if(on)n++});
  btns.forEach(b=>b.setAttribute('aria-current',String(b.dataset.cat===cat)));
  list.hidden=n===0;empty.hidden=n!==0;
  if(!n)empty.textContent='Hier staat nog niets. Zet category: '+cat+' bij een programma in software.yml.';
}
btns.forEach(b=>b.addEventListener('click',()=>{show(b.dataset.cat);history.replaceState(null,'','#'+(b.dataset.cat==='*'?'':encodeURIComponent(b.dataset.cat)))}));
if(btns.length){const h=decodeURIComponent(location.hash.slice(1));show(btns.some(b=>b.dataset.cat===h)?h:'*')}
const dlg=document.getElementById('contact');
if(dlg){
  const form=document.getElementById('cform'),st=document.getElementById('cstatus'),send=form.querySelector('[type=submit]');
  document.getElementById('ask').addEventListener('click',()=>{st.textContent='';dlg.showModal()});
  document.getElementById('cancel').addEventListener('click',()=>dlg.close());
  form.addEventListener('submit',async ev=>{
    ev.preventDefault();
    if(form.botcheck.checked)return;
    send.disabled=true;st.style.color='';st.textContent='Bezig met versturen...';
    try{
      const r=await fetch(form.dataset.endpoint,{method:'POST',headers:{Accept:'application/json'},body:new FormData(form)});
      const j=await r.json().catch(()=>({}));
      if(r.ok&&(j.success===true||j.ok===true)){form.reset();st.textContent='Bedankt, je bericht is verstuurd.';setTimeout(()=>dlg.close(),1800)}
      else throw new Error('mislukt')
    }catch(_){st.style.color='var(--red-soft)';st.textContent='Versturen is niet gelukt. Probeer het later opnieuw.'}
    send.disabled=false;
  });
}
</script></body></html>"""

DIALOG = """<dialog id="contact" aria-labelledby="ctitle">
<form id="cform" data-endpoint="__ENDPOINT__">
<h2 id="ctitle">Mis je iets?</h2>
<p>Mis je een programma, of klopt er iets niet? Laat het weten.</p>
<label>Bericht<textarea name="message" required maxlength="2000" rows="5"></textarea></label>
<label>Je naam (niet verplicht)<input name="name" maxlength="80" autocomplete="name"></label>
<label>E-mailadres, als je antwoord wilt (niet verplicht)<input type="email" name="email" maxlength="120" autocomplete="email"></label>
__KEYFIELD__
<input type="hidden" name="subject" value="Software updates: bericht van de pagina">
<input type="checkbox" name="botcheck" tabindex="-1" autocomplete="off" style="display:none">
<p id="cstatus" role="status"></p>
<div class="actions"><button class="btn" type="submit">Versturen</button><button class="btn ghost" type="button" id="cancel">Annuleren</button></div>
</form></dialog>"""

if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    run()
