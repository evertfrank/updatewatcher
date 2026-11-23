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
    for f in ("%B %d, %Y", "%d %B %Y", "%B %d %Y", "%Y-%m-%d"):
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
    (DATA / "index.html").write_text(render(out, now))
    return cfg.get("interval_hours", 6)

PALETTE = ["#c8202f", "#3a52b8", "#f3f1ea", "#ff6b78", "#6f8bff", "#9ea3b3"]  # kleur per categorie, in volgorde van software.yml
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

def row_html(i, now):
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
    return (f'<li class="row{" new" if new else ""}"><div class="who"><span class="name">{e(i["name"])}</span>{badge}{note}</div>'
            f'<div class="ver"><span class="num">{e(i["version"] or "onbekend")}</span>{was}{err}</div>'
            f'<div class="dl">{dl}</div>{more}</li>')

def render(items, now):
    e = html.escape
    cats = {}
    for i in items:
        cats.setdefault(i.get("category") or "", []).append(i)
    grouped = any(cats)            # alleen groeperen als er categorieën zijn ingevuld
    color = {c: PALETTE[n % len(PALETTE)] for n, c in enumerate(cats)}
    n_new = sum(is_new(i, now) for i in items)
    n_err = sum(bool(i["error"]) for i in items)
    if n_new:
        summary = f"{n_new} programma heeft een nieuwe versie" if n_new == 1 else f"{n_new} programma's hebben een nieuwe versie"
    else:
        summary = f"Geen nieuwe versies in de afgelopen {NEW_DAYS} dagen"
    if n_err:
        summary += f", bij {n_err} lukte de controle niet"
    summary += "."

    if not grouped:
        chips = ""
        sections = '<ul class="list">' + "".join(row_html(i, now) for i in items) + "</ul>"
    else:
        chips = '<div class="chips" role="group" aria-label="Filter op categorie"><button class="chip" data-cat="*" aria-pressed="true">Alles</button>'
        sections = ""
        for c, group in cats.items():
            name = c or "Overig"
            chips += f'<button class="chip" data-cat="{e(name)}" aria-pressed="false" style="--c:{color[c]}"><i></i>{e(name)}</button>'
            sections += (f'<section data-cat="{e(name)}" style="--c:{color[c]}"><h2>{e(name)}<span class="count">{len(group)}</span></h2>'
                         f'<ul class="list">{"".join(row_html(i, now) for i in group)}</ul></section>')
        chips += "</div>"

    return (PAGE.replace("__NOW__", e(nl_datetime(now))).replace("__SUMMARY__", e(summary))
                .replace("__CHIPS__", chips).replace("__SECTIONS__", sections))

PAGE = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Software updates</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;--display:"Big Shoulders Display","Barlow Condensed","Arial Narrow",sans-serif;
--bg:#0b0c0f;--surface:#14161b;--line:#2a2e38;--text:#f3f1ea;--muted:#9ea3b3;
--navy:#1b2a5e;--navy-line:#3a52b8;--red:#c8202f;--red-soft:#ff6b78;--btn:#f3f1ea;--btn-ink:#0b0c0f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Instrument Sans",system-ui,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:clamp(1.25rem,4vw,2.5rem) 1rem 3rem}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:.25rem 1.5rem;padding-bottom:.6rem;border-bottom:2px solid var(--red)}
h1{font:800 clamp(1.6rem,4vw,2.3rem)/1 var(--display);letter-spacing:.01em;margin:0}
.checked{color:var(--muted);font-size:.8rem;margin:0}
.summary{font-size:.95rem;margin:1rem 0 1.25rem}
.chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:0 0 1.5rem}
.chip{display:inline-flex;align-items:center;gap:.4rem;padding:.25rem .7rem;border:1px solid var(--line);border-radius:999px;background:none;color:var(--text);font:inherit;font-size:.78rem;cursor:pointer}
.chip i{width:.5rem;height:.5rem;border-radius:50%;background:var(--c)}
.chip[aria-pressed=true]{background:var(--navy);color:var(--text);border-color:var(--navy-line)}
.chip:focus-visible,.btn:focus-visible,.more summary:focus-visible,.more a:focus-visible{outline:2px solid var(--red-soft);outline-offset:2px}
section{margin:0 0 1.75rem}
h2{display:flex;align-items:baseline;gap:.5rem;margin:0 0 .5rem;font:700 1.1rem/1 var(--display)}
.count{font:500 .78rem "Instrument Sans",sans-serif;color:var(--muted)}
.list{--c:var(--navy-line);list-style:none;margin:0;padding:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.row{position:relative;display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) 11rem;gap:.5rem 1.25rem;align-items:center;padding:.7rem 1rem .7rem 1.25rem;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row::before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:var(--c)}
.row.new{--c:var(--red);background:color-mix(in srgb,var(--red) 14%,var(--surface))}
.name{font-weight:600;font-size:.95rem}
.note{display:block;color:var(--muted);font-size:.78rem}
.badge{display:inline-flex;align-items:center;gap:.35rem;margin-left:.5rem;padding:.05rem .5rem;border-radius:999px;background:var(--red);color:#fff;font-size:.7rem;font-weight:700;vertical-align:.1em}
.badge::before{content:"";width:.4rem;height:.4rem;border-radius:50%;background:#fff;box-shadow:0 0 6px #fff;animation:lamp 1.8s ease-in-out infinite}
@keyframes lamp{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.badge::before{animation:none}}
.num{display:inline-block;padding:.1rem .6rem;border:1px solid var(--navy-line);border-radius:9px;background:var(--navy);font:700 clamp(1.15rem,3vw,1.4rem)/1.2 var(--display);font-variant-numeric:tabular-nums}
.was{display:block;color:var(--muted);font-size:.75rem;margin-top:.1rem}
.err{display:block;color:var(--red-soft);font-size:.78rem;margin-top:.1rem}
.dl{display:flex;flex-wrap:wrap;gap:.4rem;justify-content:flex-end}
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.35rem .75rem;border-radius:6px;background:var(--btn);color:var(--btn-ink);font-size:.8rem;font-weight:600;text-decoration:none}
.btn:hover{background:var(--red);color:#fff}
.btn svg{width:.95em;height:.95em}
.more{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:flex-start;gap:.4rem 1.25rem;font-size:.8rem}
.more summary{cursor:pointer;font-weight:600}
.more ul{margin:.4rem 0 0;padding-left:1rem;color:var(--muted);max-width:70ch}
.more a{color:var(--text)}
footer{margin-top:1.25rem;color:var(--muted);font-size:.78rem}
@media (max-width:700px){.row{grid-template-columns:1fr}.dl{justify-content:flex-start}}
</style></head><body><div class="wrap">
<header><h1>Software updates</h1><p class="checked">Laatst gecontroleerd __NOW__</p></header>
<p class="summary">__SUMMARY__</p>
__CHIPS__
__SECTIONS__
<footer>De lijst wordt elke 6 uur automatisch bijgewerkt.</footer>
</div>
<script>
const chips=[...document.querySelectorAll('.chip')],secs=[...document.querySelectorAll('section[data-cat]')];
chips.forEach(c=>c.addEventListener('click',()=>{
  chips.forEach(x=>x.setAttribute('aria-pressed',String(x===c)));
  secs.forEach(s=>{s.hidden=c.dataset.cat!=='*'&&s.dataset.cat!==c.dataset.cat});
}));
</script></body></html>"""

if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    run()
