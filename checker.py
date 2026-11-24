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
    if re.fullmatch(r"\d{9,11}", s.strip()):   # Unix-tijd, bijvoorbeeld uit een JSON-bestand
        return datetime.datetime.fromtimestamp(int(s.strip()) + 7200, datetime.timezone.utc).strftime("%Y-%m-%d")
    s = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", s.strip().replace(" of ", " "))
    for f in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %d %Y", "%Y-%m-%d", "%d.%m.%Y"):
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
                    out.append({"label": d["label"], "url": urllib.parse.urljoin(it["url"], html.unescape(next(g for g in m.groups() if g)))})   # bij meerdere groepen telt de eerste die iets vond
                elif d.get("fallback"):
                    out.append({"label": d["label"], "url": d["fallback"]})
        except Exception:
            if d.get("fallback"):   # pagina niet bereikbaar: laatst bekende directe link gebruiken
                out.append({"label": d["label"], "url": d["fallback"]})
    return out

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 update-watcher"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read(400_000), resp.headers.get("Content-Type", "")

ICON_EXT = {"image/png": "png", "image/svg+xml": "svg", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}

def site_icon(it):
    """Haalt het pictogram op dat de website zelf gebruikt (apple-touch-icon of favicon) en bewaart het in docs/icons/. Geeft het pad terug, of '' als dat niet lukt."""
    base = it.get("icon_site") or it.get("link") or it.get("url") or ""
    host = urllib.parse.urlparse(base).netloc
    if not host:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    folder = DATA / "icons"
    for hit in folder.glob(slug + ".*") if folder.exists() else []:
        return f"icons/{hit.name}"                      # al eerder opgehaald
    try:
        page_url = f"https://{host}/"
        cands = []
        try:
            page = fetch(page_url)
            links = re.findall(r"<link[^>]+>", page, re.I)
            def rank(tag):
                rel = re.search(r'rel=["\']([^"\']+)', tag, re.I)
                rel = rel.group(1).lower() if rel else ""
                if "apple-touch-icon" in rel: return 0
                if "icon" in rel: return 1
                return None
            for tag in links:
                r_ = rank(tag)
                h = re.search(r'href=["\']([^"\']+)', tag, re.I)
                if r_ is not None and h and not h.group(1).startswith("data:"):
                    cands.append((r_, urllib.parse.urljoin(page_url, html.unescape(h.group(1)))))
        except Exception:
            pass
        cands = [u for _, u in sorted(cands, key=lambda c: c[0])] + [page_url + "favicon.ico"]
        for u in cands:
            try:
                data, ctype = fetch_bytes(u)
            except Exception:
                continue
            ext = ICON_EXT.get(ctype.split(";")[0].strip().lower()) or pathlib.PurePosixPath(urllib.parse.urlparse(u).path).suffix.lstrip(".").lower()
            if data and ext in ("png", "svg", "ico", "jpg", "jpeg", "webp", "gif"):
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{slug}.{ext}").write_bytes(data)
                print(f"OK   icoon {host}: {u}", flush=True)
                return f"icons/{slug}.{ext}"
        print(f"INFO icoon {host}: geen pictogram gevonden, lettericoon gebruikt", flush=True)
    except Exception as ex:
        print(f"INFO icoon {host}: {ex}", flush=True)
    return ""

def site_url(it):
    """Website van de fabrikant: 'website' uit software.yml, anders de hoofdpagina van icon_site/url/link."""
    if it.get("website"):
        return it["website"]
    for k in ("icon_site", "url", "link"):
        u = it.get(k)
        if u and u.startswith("http"):
            p = urllib.parse.urlparse(u)
            return f"{p.scheme}://{p.netloc}/"
    return ""

def run():
    cfg = yaml.safe_load(CFG.read_text())
    f = DATA / "versions.json"
    old = {i["name"]: i for i in json.loads(f.read_text())} if f.exists() else {}
    now = datetime.datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M")
    out = []
    for it in cfg["items"]:
        o = old.get(it["name"], {})
        r = {"name": it["name"], "category": it.get("category", ""), "icon": it.get("icon", ""), "favorite": bool(it.get("favorite")), "icon_text": it.get("icon_text", ""),
             "link": it.get("link") or it.get("url"), "website": site_url(it), "vendor": it.get("vendor") or it["name"], "strip": it.get("strip", True), "note": it.get("note", ""),
             "version": o.get("version"), "previous": o.get("previous"),
             "changed": o.get("changed"), "error": None, "checked": now}
        if it.get("alt_url"):   # eerste bron niet bereikbaar? dan de tweede proberen
            try:
                fetch(it["url"])
            except Exception as e:
                print(f"INFO {it['name']}: {it['url']} gaf {str(e)[:60]}, probeer {it['alt_url']}", flush=True)
                it = {**it, "url": it["alt_url"]}
        try:
            v, prev_page = check(it)
            if v != r["version"]:
                r["previous"], r["version"], r["changed"] = r["version"], v, now
            if prev_page:
                r["previous"] = prev_page   # echte vorige versie uit de pagina gaat voor
        except Exception as e:
            r["error"] = str(e)[:200]
            if not r["version"] and it.get("fallback_version"):   # nog nooit gelukt: laatst bekende versie tonen
                r["version"] = str(it["fallback_version"])
        print(("OK   " if not r["error"] else "FOUT ") + it["name"] + ": " + (r["error"] or r["version"]), flush=True)
        r["downloads"] = resolve_downloads(it, r["version"]) if r["version"] else []
        r["released"], r["notes"], r["notes_url"] = None, None, None
        if r["version"] and not r["error"]:
            extras(it, r)
        if not r["icon"]:
            r["icon"] = site_icon(it)
        out.append(r)
    f.write_text(json.dumps(out, indent=1))
    (DATA / "index.html").write_text(render(out, now, cfg.get("contact"), cfg.get("categories")))
    return cfg.get("interval_hours", 6)

PALETTE = ["#d4814a", "#7fb8bd", "#ebe8e2", "#e8955a", "#7fb8bd", "#8a9a9f"]  # kleur per categorie, in volgorde van software.yml
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

RECENT_DAYS = 30

def is_recent(i, now, days=RECENT_DAYS):
    """Bijgewerkt in de laatste 30 dagen: de releasedatum ligt daarin, of de checker zag de versie in die tijd veranderen."""
    try:
        today = datetime.datetime.strptime(now, FMT)
        if i.get("released"):
            d = (today - datetime.datetime.strptime(i["released"], "%Y-%m-%d")).days
            return 0 <= d < days
        if i.get("previous") and i.get("changed"):
            return (today - datetime.datetime.strptime(i["changed"], FMT)).days < days
    except Exception:
        pass
    return False

def update_date(i):
    """Releasedatum van de software, voor het sorteren van NEW UPDATES."""
    return str(i.get("released") or "")[:10]

def seen_date(i):
    """Dag dat de checker de nieuwe versie zag; alleen gebruikt als er geen releasedatum bekend is."""
    return str(i.get("changed") or "")[:10] if i.get("previous") else ""

ICON_MAC = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="10" height="7" rx="1"/><path d="M1.5 12.5h13"/></svg>'      # laptop
ICON_WIN = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="1.5" y="2.5" width="13" height="8.5" rx="1"/><path d="M8 11v2.5M5 13.5h6"/></svg>'   # desktop-scherm

ICON = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v8m0 0L4.5 6.5M8 10l3.5-3.5M3 13.5h10"/></svg>'

VENDORS = ("fabfilter", "cntrlld", "d&b", "l-acoustics", "shure")

def monogram(name):
    """Korte letters voor het icoontje, bijvoorbeeld QLab wordt QL en FabFilter Pro-Q 4 wordt PQ."""
    words = name.split()
    if len(words) > 1 and words[0].lower() in VENDORS:
        words = words[1:]
    tokens = re.findall(r"[A-Za-z]+", " ".join(words)) or [name[:2]]
    if len(tokens) > 1:
        return (tokens[0][0] + tokens[1][0]).upper()
    caps = re.findall(r"[A-Z]", tokens[0])
    return ("".join(caps[:2]) if 2 <= len(caps) <= 3 and tokens[0] != tokens[0].upper() else tokens[0][:2]).upper()

def icon_html(i):
    e = html.escape
    if i.get("icon"):
        return f'<img class="ico" src="{e(str(i["icon"]))}" alt="" width="32" height="32" loading="lazy">'
    txt = str(i.get("icon_text") or "").strip() or monogram(i["name"])
    tone = "r" if sum(map(ord, i["name"])) % 3 == 0 else "b"      # afwisselend blauw en rood
    return f'<span class="ico mono {tone}{" long" if len(txt) > 2 else ""}" aria-hidden="true">{e(txt)}</span>'

def name_html(i):
    e = html.escape
    u = i.get("website") or ""
    if not u.startswith("http"):
        return f'<span class="name">{e(i["name"])}</span>'
    return f'<a class="name" href="{e(u)}" target="_blank" rel="noopener" title="Naar de website van de fabrikant">{e(i["name"])}</a>'

def vendors_html(items):
    """Rij icoontjes per fabrikant boven de zoekbalk; klikken toont alleen die fabrikant."""
    e = html.escape
    groups = {}
    for i in items:
        if str(i.get("category", "")).upper() == "PLUGINS" or i.get("strip") is False:
            continue          # plugins en programma's met strip: false komen niet in de icoontjesrij
        groups.setdefault(i.get("vendor") or i["name"], []).append(i)
    out = []
    for v, its in sorted(groups.items(), key=lambda g: g[0].casefold()):   # alfabetisch
        src = next((i for i in its if i.get("icon")), None)
        if not src:          # geen echte afbeelding gevonden: niet tonen in de rij
            continue
        ico = icon_html({"icon": src["icon"], "name": v})
        n = len(its)
        cnt = f'<b>{n}</b>' if n > 1 else ""
        label = f"{v}: {n} programma's" if n > 1 else f"{v}: {its[0]['name']}"
        out.append(f'<button type="button" data-v="{e(v)}" aria-pressed="false" title="{e(label)}" aria-label="{e(label)}">{ico}{cnt}</button>')
    if not out:
        return ""
    return f'<div class="vendors" role="group" aria-label="Fabrikanten">{"".join(out)}</div>'

def row_html(i, now, cat=""):
    e = html.escape
    new = is_new(i, now)
    badge = '<span class="badge">Nieuw</span>' if new else ""
    star = '<svg class="star" viewBox="0 0 20 20" role="img" aria-label="Favoriet"><path d="M10 1.8l2.4 5.3 5.8.6-4.3 3.9 1.2 5.7L10 14.5l-5.1 2.8 1.2-5.7L1.8 7.7l5.8-.6z"/></svg>' if i.get("favorite") else ""
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
        lab = lambda d: d["label"].lower()
        def btn(d):
            if lab(d).startswith("mac"):
                return f'<a class="btn os" href="{e(d["url"])}">{ICON_MAC}{e(d["label"])}</a>'
            if lab(d).startswith("win"):
                return f'<a class="btn os" href="{e(d["url"])}">{ICON_WIN}{e(d["label"])}</a>'
            return f'<a class="btn gen" href="{e(d["url"])}">{ICON}{e(d["label"])}</a>'   # algemene download
        mac = [d for d in i["downloads"] if lab(d).startswith("mac")]
        win = [d for d in i["downloads"] if lab(d).startswith("win")]
        rest = [d for d in i["downloads"] if d not in mac and d not in win]
        # twee vaste plekken: links Mac (ook als Windows ontbreekt), rechts Windows, zodat de knoppen onder elkaar staan
        dl = f'<span class="slot">{"".join(btn(d) for d in mac + rest)}</span><span class="slot">{"".join(btn(d) for d in win)}</span>'
    else:
        dl = f'<span class="slot"><a class="btn gen" href="{e(i["link"] or "#")}">{ICON}Download</a></span><span class="slot"></span>'
    more = ""
    if i.get("notes"):
        more += '<details><summary>Wijzigingen</summary><ul>' + "".join(f"<li>{e(l)}</li>" for l in i["notes"]) + "</ul></details>"
    if i.get("notes_url"):
        more += f'<a href="{e(i["notes_url"])}" target="_blank" rel="noopener">{e(i["notes_label"])}</a>'
    more = f'<div class="more">{more}</div>' if more else ""
    return (f'<li class="row{" new" if new else ""}" data-cat="{e(cat)}" data-fav="{1 if i.get("favorite") else 0}" data-recent="{1 if is_recent(i, now) else 0}" data-date="{e(update_date(i))}" data-seen="{e(seen_date(i))}" data-vendor="{e(i.get("vendor") or i["name"])}"><div class="who">{icon_html(i)}<div>{name_html(i)}{badge}{note}</div></div>'
            f'<div class="ver"><span class="num">{e(i["version"] or "onbekend")}</span>{was}{err}</div>'
            f'<div class="dl">{dl}</div>{more}</li>')

def render(items, now, contact=None, categories=None):
    e = html.escape
    # categorieën uit software.yml (volgorde = volgorde van de zijbalk); een categorie die alleen bij een programma staat komt erbij
    canon = {}
    for c in (categories or []):
        if str(c).strip() and str(c).strip().lower() != "favorites":
            canon.setdefault(str(c).strip().lower(), str(c).strip())
    for i in items:
        c = str(i.get("category") or "").strip()
        if c and c.lower() != "favorites":
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

    rows = "".join(row_html(i, now, cat_of(i)) for i in sorted(items, key=lambda i: i["name"].casefold()))   # alfabetisch
    search = '<div class="search"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="5.5"/><path d="M13.5 13.5L17 17"/></svg><input id="q" type="search" placeholder="Zoek in software" aria-label="Zoek in software" autocomplete="off" spellcheck="false"></div>'
    listing = f'{vendors_html(items)}{search}<ul class="list">{rows}</ul><p class="empty" id="empty" hidden></p>'
    if canon:
        nrec = sum(is_recent(i, now) for i in items)
        nav = f'<button type="button" class="rec" data-cat="RECENT" aria-current="false" title="Updates van de afgelopen 30 dagen, nieuwste bovenaan"><span>NEW UPDATES<small>Updates van de afgelopen 30 dagen</small></span><b>{nrec}</b></button>'
        nav += f'<button type="button" data-cat="*" aria-current="true"><span>ALL</span><b>{len(items)}</b></button>'
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
                .replace("__SUMMARY__", e(summary)).replace("__LOGO__", logo_svg()).replace("__SW__", "".join(f"<i>{ch}</i>" for ch in "SOFTWARE")).replace("__FAVICON__", "data:image/svg+xml," + urllib.parse.quote(logo_svg(tile=True).replace(' class="logo"', '').replace("<svg ", "<svg xmlns='http://www.w3.org/2000/svg' ", 1))).replace("__LAYOUT__", layout))

import math

def logo_svg(tile=False, cls="logo", lit_col="#7fb8bd", hot_col="#d4814a", dim_col="#34484e", needle_col="#ebe8e2", tile_col="#121b1f"):
    """Het merkteken: een ring van lichtjes zoals op een compressor of niveaumeter. Petrol brandt, de laatste oranje (er is iets nieuws), de naald wijst naar de nieuwste stand."""
    n, lit, hot, start, span = 18, 14, 16, 135, 270
    parts = []
    for i in range(n):
        a = math.radians(start + i * span / (n - 1))
        x1, y1, x2, y2 = 32 + 21 * math.cos(a), 32 + 21 * math.sin(a), 32 + 27.5 * math.cos(a), 32 + 27.5 * math.sin(a)
        if i < lit:   col, op = lit_col, .35 + .65 * i / (lit - 1)
        elif i < hot: col, op = hot_col, 1
        else:         col, op = dim_col, 1
        parts.append(f'<path d="M{x1:.2f} {y1:.2f}L{x2:.2f} {y2:.2f}" stroke="{col}" stroke-opacity="{op:.2f}" stroke-width="3.4" stroke-linecap="round"/>')
    a = math.radians(start + (lit) * span / (n - 1))
    nx, ny = 32 + 15 * math.cos(a), 32 + 15 * math.sin(a)
    needle = f'<path d="M32 32L{nx:.2f} {ny:.2f}" stroke="{needle_col}" stroke-width="3" stroke-linecap="round"/><circle cx="32" cy="32" r="3.6" fill="{needle_col}"/>'
    bg = '<rect width="64" height="64" rx="15" fill="{tile_col}"/>' if tile else ""
    return f'<svg class="{cls}" viewBox="0 0 64 64" aria-hidden="true">{bg}{"".join(parts)}{needle}</svg>'

PAGE = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Software Update Watcher</title>
<link rel="icon" href="__FAVICON__">
<meta name="description" content="Nieuwste versies van live-audio software, met directe downloadlinks.">
<meta property="og:title" content="Software Update Watcher">
<meta property="og:description" content="Nieuwste versies van live-audio software, met directe downloadlinks.">
<meta property="og:image" content="https://evertfrank.github.io/updatewatcher/logo-share.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@700;800&family=Instrument+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{color-scheme:dark;--display:"Poppins","Arial Black","Helvetica Neue",Arial,sans-serif;
--bg:#0c1316;--surface:#121b1f;--raise:#182329;--line:#25343a;--text:#ebe8e2;--muted:#8a9a9f;
--blue:#7fb8bd;--blue-deep:#1d3b42;--red:#d4814a;--red-deep:#3d2616;--red-soft:#e8955a;--gold:#a99bd6;--gold-deep:#2c2745}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;min-height:100vh;color:var(--text);font:14px/1.5 "Instrument Sans",system-ui,sans-serif;
background:radial-gradient(60rem 22rem at 12% -6rem,color-mix(in srgb,var(--blue-deep) 70%,transparent),transparent 70%),radial-gradient(40rem 18rem at 100% -4rem,color-mix(in srgb,var(--red-deep) 55%,transparent),transparent 70%),var(--bg)}
.wrap{max-width:1080px;margin:0 auto;padding:clamp(1.25rem,4vw,2.75rem) clamp(1rem,3vw,2rem) 3rem}
header{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:1rem 1.5rem}
.banner{padding:clamp(1.1rem,3vw,1.9rem) clamp(1.1rem,3.5vw,2.2rem);border:1px solid color-mix(in srgb,var(--blue) 22%,var(--line));border-radius:22px;background:radial-gradient(70% 140% at 14% 50%,#2c5560 0,#17323a 58%,#0f1d22 100%);box-shadow:0 18px 50px -28px #000}
.brand{display:flex;align-items:center;gap:clamp(1rem,3vw,1.9rem)}
.word{display:flex;flex-direction:column;gap:.55rem}
h1{display:flex;flex-direction:column;gap:.6rem;margin:0;font-family:var(--display);text-transform:uppercase}
h1 .sw{display:flex;justify-content:space-between;font:700 clamp(.78rem,2.3vw,1.05rem)/1 var(--display);color:#a6d3d6}
h1 .sw i{font-style:normal}
h1 .uw{font:800 clamp(1.45rem,5vw,2.7rem)/.95 var(--display);letter-spacing:-.015em;white-space:nowrap;color:var(--text)}
h1 em{font-style:normal;color:#e8955a}
.by{margin:0;color:#8a9a9f;font-size:.8rem;font-weight:500}
.logo{width:clamp(3.4rem,11vw,6rem);height:auto;flex:none}
.banner .checked{color:#8fb6ba}
.banner .ask{border-color:#e8955a}
.checked{color:var(--muted);font-size:.78rem;margin:0}
.summary{color:var(--muted);font-size:.9rem;margin:1.25rem 0 1.75rem}
.layout{display:grid;grid-template-columns:13.5rem minmax(0,1fr);gap:2.5rem;align-items:start}
.cats{position:sticky;top:1.5rem;display:flex;flex-direction:column;gap:.2rem;padding-left:.25rem;border-left:1px solid var(--line)}
.cats button{position:relative;display:flex;justify-content:space-between;align-items:center;gap:.5rem;width:100%;padding:.7rem .95rem;border:0;border-radius:10px;background:none;color:var(--muted);font:600 .98rem/1.2 "Instrument Sans",sans-serif;text-align:left;cursor:pointer;transition:color .15s,background .15s}
.cats button b{font-weight:500;font-size:.82rem;color:var(--muted)}
.cats button:hover{color:var(--text);background:color-mix(in srgb,var(--raise) 60%,transparent)}
.cats button[aria-current=true]{background:var(--raise);color:var(--text)}
.cats button[aria-current=true]::before{content:"";position:absolute;left:-.5rem;top:25%;bottom:25%;width:2px;border-radius:2px;background:var(--red)}
.cats button.fav{color:color-mix(in srgb,var(--gold) 80%,var(--muted))}
.cats button.fav b{color:var(--gold)}
.cats button.fav[aria-current=true]{background:var(--gold-deep);color:var(--gold)}
.cats button.fav[aria-current=true]::before{background:var(--gold)}
.cats button.rec{margin-bottom:.45rem}
.cats button.rec::after{content:"";position:absolute;left:0;right:.6rem;bottom:-.3rem;height:1px;background:var(--line)}
.cats button.rec{color:color-mix(in srgb,var(--red-soft) 70%,var(--muted))}
.cats button.rec b{color:var(--red-soft)}
.cats button.rec[aria-current=true]{background:var(--red-deep);color:var(--red-soft)}
.cats button.rec[aria-current=true]::before{background:var(--red)}
.cats button span{text-align:left}
.cats button small{display:block;margin-top:.2rem;font:500 .72rem/1.3 "Instrument Sans",system-ui,sans-serif;letter-spacing:0;text-transform:none;color:var(--muted)}
.cats button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.layout>*{min-width:0}
.vendors{display:flex;flex-wrap:nowrap;gap:.45rem;margin:0 0 .9rem;padding:.4rem .1rem .3rem;overflow-x:auto;scrollbar-width:none}.vendors::-webkit-scrollbar{display:none}
.vendors button{position:relative;display:grid;place-items:center;flex:0 1 2.65rem;min-width:2.1rem;aspect-ratio:1;padding:0;border:1px solid var(--line);border-radius:11px;background:var(--surface);cursor:pointer;transition:border-color .15s,background .15s,transform .15s}
.vendors button .ico{width:74%;height:74%;border-radius:7px}
.vendors button:hover{border-color:color-mix(in srgb,var(--blue) 50%,var(--line));transform:translateY(-1px)}
.vendors button[aria-pressed=true]{border-color:var(--red);background:var(--red-deep);box-shadow:0 0 0 1px var(--red)}
.vendors button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.vendors button b{position:absolute;right:-.35rem;top:-.35rem;min-width:1.1rem;height:1.1rem;padding:0 .25rem;border-radius:999px;background:var(--raise);border:1px solid var(--line);color:var(--muted);font:600 .64rem/1rem "Instrument Sans",system-ui,sans-serif;text-align:center}
.vendors button[aria-pressed=true] b{background:var(--red);border-color:var(--red);color:var(--bg)}
@media (prefers-reduced-motion:reduce){.vendors button{transition:none}.vendors button:hover{transform:none}}
.search{position:relative;margin:0 0 .5rem}
.search svg{position:absolute;left:.9rem;top:50%;width:1rem;height:1rem;transform:translateY(-50%);fill:none;stroke:var(--muted);stroke-width:1.8;stroke-linecap:round;pointer-events:none}
.search input{width:100%;padding:.65rem 1rem .65rem 2.6rem;border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--text);font:inherit;font-size:.9rem;transition:border-color .15s,background .15s}
.search input::placeholder{color:var(--muted)}
.search input:hover{border-color:color-mix(in srgb,var(--blue) 40%,var(--line))}
.search input:focus-visible{outline:2px solid var(--blue);outline-offset:2px;border-color:transparent}
.list{list-style:none;margin:0;padding:0}
.row{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 11rem 13.6rem;gap:.4rem 1.5rem;align-items:center;padding:.95rem .25rem;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row.new::before{content:"";position:absolute;inset:.3rem -.75rem;z-index:-1;border-radius:12px;background:linear-gradient(90deg,color-mix(in srgb,var(--red-deep) 70%,transparent),transparent 80%)}
.who{display:flex;align-items:center;gap:.85rem;min-width:0}
.ico{flex:none;width:2.1rem;height:2.1rem;border-radius:10px;object-fit:contain}
img.ico{padding:.3rem;background:var(--text)}
.mono{display:grid;place-items:center;font:800 .72rem/1 var(--display);letter-spacing:.02em}
.mono.long{font-size:.6rem;letter-spacing:0}
.mono.b{background:var(--blue-deep);color:var(--blue);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--blue) 30%,transparent)}
.mono.r{background:var(--red-deep);color:var(--red-soft);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--red) 35%,transparent)}
.name{font-weight:600;font-size:.95rem;color:inherit;text-decoration:none}
a.name{text-decoration:underline;text-decoration-color:transparent;text-underline-offset:.2em;text-decoration-thickness:1.5px;transition:text-decoration-color .15s,color .15s}
a.name:hover,a.name:focus-visible{color:var(--blue);text-decoration-color:currentColor}
.star{width:.8rem;height:.8rem;margin-left:.45rem;fill:var(--gold);vertical-align:-.02em}
.note{display:block;color:var(--muted);font-size:.76rem;margin-top:.1rem}
.badge{display:inline-flex;align-items:center;gap:.4rem;margin-left:.6rem;padding:.08rem .55rem;border-radius:999px;background:var(--red-deep);color:var(--red-soft);font-size:.68rem;font-weight:600;vertical-align:.12em}
.badge::before{content:"";width:.38rem;height:.38rem;border-radius:50%;background:var(--red);animation:lamp 1.8s ease-in-out infinite}
@keyframes lamp{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.badge::before{animation:none}}
.num{display:inline-block;color:var(--blue);font:700 1.05rem/1.2 var(--display);letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.was{display:block;color:var(--muted);font-size:.74rem;margin-top:.05rem}
.err{display:block;color:var(--red-soft);font-size:.76rem;margin-top:.1rem}
.dl{display:grid;grid-template-columns:repeat(2,6.6rem);gap:.4rem;justify-content:end}
.slot{display:flex;flex-direction:column;gap:.4rem}
.slot .btn{justify-content:center}
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.38rem .85rem;border:1px solid color-mix(in srgb,var(--blue) 45%,transparent);border-radius:999px;background:none;color:var(--text);font-size:.78rem;font-weight:600;text-decoration:none;transition:background .15s,color .15s,border-color .15s}
.btn:hover{background:var(--blue);border-color:var(--blue);color:#0c1316}
.btn:focus-visible,.more summary:focus-visible,.more a:focus-visible,.ask:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.btn svg{width:1.05em;height:1.05em;flex:none}
.btn.gen{border-color:color-mix(in srgb,var(--gold) 55%,transparent);color:color-mix(in srgb,var(--gold) 35%,var(--text))}
.btn.gen:hover{background:var(--gold);border-color:var(--gold);color:#0c1316}
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
.row{grid-template-columns:1fr}.dl{justify-content:start}
.cats button.rec::after{display:none}.cats button small{display:none}
.vendors{flex-wrap:nowrap;overflow-x:auto;padding:.4rem .1rem .3rem;margin-left:-.1rem;scrollbar-width:none}.vendors::-webkit-scrollbar{display:none}}
</style></head><body><div class="wrap">
<header class="banner"><div class="brand">__LOGO__<div class="word"><h1 aria-label="Software Update Watcher"><span class="sw" aria-hidden="true">__SW__</span><span class="uw" aria-hidden="true">Update <em>Watcher</em></span></h1><p class="by">by evertfrank</p></div></div><div class="side"><p class="checked">Laatst gecontroleerd __NOW__</p>__ASK__</div></header>
<p class="summary">__SUMMARY__</p>
__LAYOUT__
<footer>De lijst wordt elke 6 uur automatisch bijgewerkt.</footer>
</div>
__DIALOG__
<script>
const btns=[...document.querySelectorAll('.cats button')],rows=[...document.querySelectorAll('.row')],list=document.querySelector('.list'),empty=document.getElementById('empty');
let cat='*';
const q=document.getElementById('q');
const vbtns=[...document.querySelectorAll('.vendors button')];
const byDate=[...rows].sort((a,b)=>{const ra=a.dataset.date||'',rb=b.dataset.date||'';if(!!ra!==!!rb)return ra?-1:1;return rb.localeCompare(ra)||(b.dataset.seen||'').localeCompare(a.dataset.seen||'')});   // releasedatum, nieuwste eerst; zonder releasedatum onderaan
function show(c){
  if(c!==undefined)cat=c;
  const term=q.value.trim().toLowerCase(),eff=term?'*':cat;   // zoeken doet altijd ALL
  let n=0;
  rows.forEach(r=>{const on=(eff==='*'||(eff.startsWith('V:')?r.dataset.vendor===eff.slice(2):eff==='RECENT'?r.dataset.recent==='1':r.dataset.cat===eff))&&(!term||r.textContent.toLowerCase().includes(term));r.hidden=!on;if(on)n++});
  (eff==='RECENT'?byDate:rows).forEach(r=>list.appendChild(r));
  btns.forEach(b=>b.setAttribute('aria-current',String(b.dataset.cat===eff)));
  vbtns.forEach(b=>b.setAttribute('aria-pressed',String('V:'+b.dataset.v===eff)));
  list.hidden=n===0;empty.hidden=n!==0;
  if(!n)empty.textContent=term?'Niets gevonden voor "'+q.value.trim()+'".':(cat==='RECENT'?'Geen updates in de afgelopen 30 dagen.':cat==='FAVORITES'?'Nog geen favorieten. Zet favorite: true bij een programma in software.yml.':'Hier staat nog niets. Zet category: '+cat+' bij een programma in software.yml.');
}
btns.forEach(b=>b.addEventListener('click',()=>{q.value='';show(b.dataset.cat);history.replaceState(null,'','#'+(b.dataset.cat==='*'?'':encodeURIComponent(b.dataset.cat)))}));
vbtns.forEach(b=>b.addEventListener('click',()=>{const c='V:'+b.dataset.v;q.value='';const nc=cat===c?'*':c;show(nc);history.replaceState(null,'','#'+(nc==='*'?'':encodeURIComponent(nc)))}));
q.addEventListener('input',()=>show());
document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==q&&!/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}else if(e.key==='Escape'&&document.activeElement===q){q.value='';show();q.blur()}});
{const h=decodeURIComponent(location.hash.slice(1));show(btns.some(b=>b.dataset.cat===h)||vbtns.some(b=>'V:'+b.dataset.v===h)?h:'*')}
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
