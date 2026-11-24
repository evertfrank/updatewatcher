import base64, json, re, socket, sys, time, threading, urllib.request, html, datetime, pathlib, http.server, functools
import yaml
import urllib.parse
from zoneinfo import ZoneInfo

CFG = pathlib.Path("software.yml")
DATA = pathlib.Path("docs")

def vkey(v): return tuple(int(x) for x in v.split("."))

LAX_TLS_HOSTS = set()   # hosts met insecure_tls: true in software.yml (server stuurt een onvolledige certificaatketen)

def tls_for(url):
    """Normaal volledige certificaatcontrole; alleen voor uitdrukkelijk genoemde hosts zonder controle."""
    import ssl
    host = urllib.parse.urlparse(url).hostname or ""
    if host in LAX_TLS_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None

def pdf_titles(data):
    """Leest de inhoudsopgave (bladwijzers) van een pdf zonder extra bibliotheek: daar staan vaak versie en datum in."""
    raw = data.decode("latin-1")
    out = []
    for m in re.finditer(r"/Title\s*(\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>)", raw):
        v = m.group(1)
        if v.startswith("<"):
            b = bytes.fromhex(re.sub(r"\s", "", v[1:-1]))
        else:
            b = re.sub(r"\\([0-7]{1,3}|.)", lambda e: chr(int(e.group(1), 8)) if e.group(1)[0] in "01234567" else
                       {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(e.group(1), e.group(1)), v[1:-1], flags=re.S).encode("latin-1", "replace")
        out.append(b[2:].decode("utf-16-be", "replace") if b[:2] == b"\xfe\xff" else b.decode("latin-1"))
    return "\n".join(out)


def open_v4_retry(req, **kw):
    """Netwerkfout (bv. 'Network is unreachable' via IPv6 op GitHub)? Eén keer opnieuw, alleen via IPv4."""
    try:
        return urllib.request.urlopen(req, **kw)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), OSError):
            raise
        orig = socket.getaddrinfo
        def v4(host, port, family=0, *a, **k):
            return orig(host, port, socket.AF_INET, *a, **k)
        socket.getaddrinfo = v4
        try:
            return urllib.request.urlopen(req, **kw)
        finally:
            socket.getaddrinfo = orig

@functools.lru_cache(maxsize=None)
def fetch(url):
    if url.startswith("pdftitles:"):   # bladwijzers van een pdf als tekst
        req = urllib.request.Request(url[10:], headers={"User-Agent": "Mozilla/5.0 update-watcher"})
        return pdf_titles(urllib.request.urlopen(req, timeout=60).read())
    if url.startswith("nuxt:"):   # de gegevens achter een Nuxt-pagina (_payload.json)
        real = url[5:]
        m = re.search(r'data-src="([^"]*_payload\.json[^"]*)"', fetch(real))
        if not m:
            raise ValueError(f"geen _payload.json gevonden op {real}")
        return fetch(urllib.parse.urljoin(real, html.unescape(m.group(1))))
    if url.startswith("b64:"):   # "b64:<attribuut>:<url>": de pagina bewaart zijn gegevens base64-gecodeerd in dat attribuut
        _, attr, real = url.split(":", 2)
        m = re.search(re.escape(attr) + r'="([A-Za-z0-9+/=]+)"', fetch(real))
        if not m:
            raise ValueError(f"attribuut {attr} niet gevonden op {real}")
        return base64.b64decode(m.group(1)).decode("utf-8", "replace")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 update-watcher"})
    return open_v4_retry(req, timeout=30, context=tls_for(url)).read().decode("utf-8", "replace")

def debug_dump(name, page):
    print(f"--- DEBUG {name}: {len(page)} tekens ontvangen", flush=True)
    if len(page) < 3000:   # korte reactie: waarschijnlijk een blokkade- of doorverwijspagina, laat zien wat erin staat
        print("     INHOUD", " ".join(re.sub(r"<[^>]+>", " ", page).split())[:400], flush=True)
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
    if it.get("url") and it.get("versions"):   # meerdere onderdelen in één regel, bijv. "Mac 4.34 · Windows 1.0.33"
        parts = []
        for sub in it["versions"]:
            v, _ = page_version({**it, **sub, "previous_from_page": False})
            parts.append(f'{sub["label"]} {v}')
        return " \u00b7 ".join(parts), None
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
    for f in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%B %d %Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    for f in ("%B %Y", "%b %Y", "%Y %B", "%Y/%m"):   # alleen maand bekend, bijvoorbeeld "July 2026" of "2026/10" uit een downloadpad
        try:
            return datetime.datetime.strptime(s, f).strftime("%Y-%m")
        except ValueError:
            pass
    return None

LABELS = {"fixed", "fix", "new", "improved", "improvement", "changed", "added", "removed", "feature"}

def strip_html(h):
    h = re.sub(r"</?(?:li|p|div|br|span|h\d|tr|td|section|article)\b[^>]*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    lines = [html.unescape(" ".join(l.split())) for l in h.split("\n")]
    lines = [re.sub(r"\s*\[r=\w+\]|\s*\u2022$", "", l).strip() for l in lines]   # linkbolletjes en interne codes weg
    lines = [l for l in lines if l and l != "\u2022"]
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

def vpat(p, ver):
    """Vult de versie in een regex in: {version} = 2.10, {version_us} = 2_10, {version_nodots} = 210."""
    return (p.replace("{version_us}", ver.replace(".", "_")).replace("{version_nodots}", ver.replace(".", ""))
             .replace("{version_dash}", ver.replace(".", "-"))
             .replace("{version}", re.escape(ver)))

def extras(it, r):
    ver, name = r["version"], r["name"]
    released = None
    if str(it.get("released", "")).strip():                       # handmatig ingevuld
        released = parse_date(str(it["released"])) or str(it["released"]).strip()
    elif it.get("date_regex"):
        try:
            date_url = fill(it.get("date_url", it.get("url", "")), ver)
            if it.get("date_link"):   # adres van de datumbron eerst opzoeken op een pagina (bijv. de pdf met release notes)
                lp = it["date_link"]["page"]
                lm = re.search(vpat(it["date_link"]["regex"], ver), fetch(lp))
                if not lm:
                    raise ValueError("link naar release notes niet gevonden")
                date_url = ("pdftitles:" if it["date_link"].get("pdf") else "") + urllib.parse.urljoin(lp, html.unescape(lm.group(1)))
            page = fetch(date_url)
            found = []
            for dr in (it["date_regex"] if isinstance(it["date_regex"], list) else [it["date_regex"]]):   # meerdere: nieuwste telt
                m = re.search(vpat(dr, ver), page, re.S)
                d = parse_date(next((g for g in m.groups() if g), "")) if m else None
                if d:
                    found.append(d)
            released = max(found) if found else None
            if not released:
                hint(name, "releasedatum", page, ver)
        except Exception as ex:
            print(f"INFO {name}: releasedatum ophalen mislukt: {ex}", flush=True)
    r["released"] = released
    notes = None
    if it.get("notes_regex"):
        try:
            page = fetch(fill(it.get("notes_page", it.get("url", "")), ver))
            m = re.search(vpat(it["notes_regex"], ver), page, re.S)
            block = next((g for g in m.groups() if g), "") if m else ""
            if block and it.get("notes_json"):      # tekst staat als JSON-string in de pagina
                block = json.loads('"' + block + '"')
            if block and it.get("notes_strings"):   # losse JSON-teksten die met een spatie beginnen (Nuxt-paginagegevens)
                notes = [json.loads('"' + x + '"').strip() for x in re.findall(r'"( [^"\\]*(?:\\.[^"\\]*)*)"', block)][:30]
            else:
                notes = strip_html(block) if block else None
            if not notes:
                hint(name, "wijzigingen", page, ver, needle="<h1")
        except Exception as ex:
            print(f"INFO {name}: wijzigingen ophalen mislukt: {ex}", flush=True)
    r["notes"] = notes or None
    r["notes_url"] = fill(it["notes_url"], ver) if it.get("notes_url") else None
    if it.get("notes_link"):   # adres van de release notes eerst opzoeken (bijv. een pdf in een downloadlijst)
        try:
            lp = it["notes_link"]["page"]
            lm = re.search(vpat(it["notes_link"]["regex"], ver), fetch(lp), re.S)
            if lm:
                r["notes_url"] = urllib.parse.urljoin(lp, html.unescape(lm.group(1)))
        except Exception as ex:
            print(f"INFO {name}: link naar release notes niet gevonden: {ex}", flush=True)
    r["notes_label"] = it.get("notes_label", "Release notes")

def resolve_downloads(it, ver):
    out = []
    for d in it.get("downloads", []):
        try:
            if "url" in d:  # vaste link met {version}
                out.append({"label": d["label"], "url": d["url"].replace("{version}", ver)})
            else:           # link uit de pagina halen met een regex (één groep = de href)
                m = re.search(vpat(d["regex"], ver), fetch(d.get("page") or it["url"]))   # page: andere pagina voor deze knop
                if m:
                    out.append({"label": d["label"], "url": urllib.parse.urljoin(it.get("link") if it["url"].startswith("b64:") else it["url"], html.unescape(next(g for g in m.groups() if g))).replace(" ", "%20")})   # bij meerdere groepen telt de eerste die iets vond
                elif d.get("fallback"):
                    out.append({"label": d["label"], "url": d["fallback"]})
        except Exception:
            if d.get("fallback"):   # pagina niet bereikbaar: laatst bekende directe link gebruiken
                out.append({"label": d["label"], "url": d["fallback"]})
    return out

def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 update-watcher"})
    with urllib.request.urlopen(req, timeout=20, context=tls_for(url)) as resp:
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
    LAX_TLS_HOSTS.update(urllib.parse.urlparse(u).hostname for it in cfg.get("items", []) if it.get("insecure_tls")
                         for u in (it.get("url"), it.get("icon_site"), it.get("link")) if u and u.startswith("http"))
    f = DATA / "versions.json"
    old = {i["name"]: i for i in json.loads(f.read_text())} if f.exists() else {}
    now = datetime.datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M")
    out = []
    for it in cfg["items"]:
        o = dict(old.get(it["name"], {}))
        if o and not o.get("tracked"):   # eenmalig: oude data telde de eerste controle ten onrechte als "nieuwe versie"
            o["changed"] = None
        r = {"name": it["name"], "category": it.get("category", ""), "icon": it.get("icon", ""), "favorite": bool(it.get("favorite")), "icon_text": it.get("icon_text", ""),
             "link": it.get("link") or it.get("url"), "website": site_url(it), "vendor": it.get("vendor") or it["name"], "strip": it.get("strip", True), "note": it.get("note", ""),
             "version": o.get("version"), "previous": o.get("previous"),
             "changed": o.get("changed"), "error": None, "checked": now, "tracked": True}
        if it.get("alt_url"):   # eerste bron niet bereikbaar? dan de tweede proberen
            try:
                fetch(it["url"])
            except Exception as e:
                print(f"INFO {it['name']}: {it['url']} gaf {str(e)[:60]}, probeer {it['alt_url']}", flush=True)
                it = {**it, "url": it["alt_url"]}
        try:
            v, prev_page = check(it)
            if r["version"] is None:            # eerste keer gezien: geen update, alleen vastleggen
                r["version"] = v
            elif v != r["version"]:             # echte nieuwe versie
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
        if o.get("version") and o.get("version") == r["version"]:   # bron (tijdelijk) onbereikbaar: eerder gevonden gegevens bewaren
            for k in ("released", "notes", "notes_url", "notes_label"):
                if not r.get(k) and o.get(k):
                    r[k] = o[k]
            if not r["downloads"] and o.get("downloads"):
                r["downloads"] = o["downloads"]
        if not r["icon"]:
            r["icon"] = site_icon(it)
        out.append(r)
    f.write_text(json.dumps(out, indent=1))
    (DATA / "index.html").write_text(render(out, now, cfg.get("contact"), cfg.get("categories"), cfg.get("strip_vendors")))
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
        if len(s) == 7:   # alleen maand bekend
            d = datetime.datetime.strptime(s, "%Y-%m")
            return f"{MONTHS[d.month - 1]} {d.year}"
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
            r = i["released"]
            d = (today - datetime.datetime.strptime(r + "-01" if len(r) == 7 else r, "%Y-%m-%d")).days   # alleen maand: telt vanaf de 1e
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

ICON_MAC = '<svg class="apple" viewBox="2.4 1 11 13.8" aria-hidden="true"><path fill="currentColor" d="M8 5C6.8 4.1 5.3 4 4.2 4.7 2.6 5.7 2.3 7.9 2.9 10.1 3.6 12.5 5.1 14.5 6.5 14.5c.7 0 1-.4 1.5-.4s.8.4 1.5.4c1.4 0 2.9-2 3.6-4.4.6-2.2.3-4.4-1.3-5.4C10.7 4 9.2 4.1 8 5Z"/><path d="M8 5.1V3.4c0-.9.3-1.6.9-2.1" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>'      # wit appeltje (fruit) met steeltje
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

def vendors_html(items, strip=None):
    """Rij icoontjes per fabrikant boven de zoekbalk; klikken toont alleen die fabrikant."""
    e = html.escape
    groups = {}
    for i in items:
        if str(i.get("category", "")).upper() == "PLUGINS" or i.get("strip") is False:
            continue          # plugins en programma's met strip: false komen niet in de icoontjesrij
        groups.setdefault(i.get("vendor") or i["name"], []).append(i)
    out = []
    if strip:   # alleen de fabrikanten uit strip_vendors in software.yml, in die volgorde
        order = [str(x).casefold() for x in strip]
        pairs = sorted(((v, its) for v, its in groups.items() if v.casefold() in order), key=lambda g: order.index(g[0].casefold()))
    else:
        pairs = sorted(groups.items(), key=lambda g: g[0].casefold())   # alfabetisch
    for v, its in pairs:
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

def num_html(v):
    """Versienummer; meerdere onderdelen ("Mac 4.34 · Windows 1.0.33") onder elkaar."""
    e = html.escape
    if v and " \u00b7 " in v:
        return '<span class="num multi">' + "".join(f"<span>{e(p)}</span>" for p in v.split(" \u00b7 ")) + "</span>"
    return f'<span class="num">{e(v or "onbekend")}</span>'

MINE = ('<button type="button" class="mine" aria-pressed="false" title="Toevoegen aan Mijn favorieten" aria-label="Toevoegen aan Mijn favorieten">'
        '<svg viewBox="0 0 16 16" aria-hidden="true"><path class="plus" d="M8 3.5v9M3.5 8h9"/><path class="tick" d="M3.5 8.5l3 3 6-7"/></svg></button>')

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
                return f'<a class="btn os" href="{e(d["url"])}" target="_blank" rel="noopener">{ICON_MAC}{e(d["label"])}</a>'
            if lab(d).startswith("win"):
                return f'<a class="btn os" href="{e(d["url"])}" target="_blank" rel="noopener">{ICON_WIN}{e(d["label"])}</a>'
            return f'<a class="btn gen" href="{e(d["url"])}" target="_blank" rel="noopener">{ICON}{e(d["label"])}</a>'   # algemene download
        mac = [d for d in i["downloads"] if lab(d).startswith("mac")]
        win = [d for d in i["downloads"] if lab(d).startswith("win")]
        rest = [d for d in i["downloads"] if d not in mac and d not in win]
        # twee vaste plekken: links Mac (ook als Windows ontbreekt), rechts Windows, zodat de knoppen onder elkaar staan
        dl = f'<span class="slot">{"".join(btn(d) for d in mac + rest)}</span><span class="slot">{"".join(btn(d) for d in win)}</span>'
    else:
        dl = f'<span class="slot"><a class="btn gen" href="{e(i["link"] or "#")}" target="_blank" rel="noopener">{ICON}Download</a></span><span class="slot"></span>'
    more = ""
    if i.get("notes"):
        more += '<details><summary>Wijzigingen</summary><ul>' + "".join(f"<li>{e(l)}</li>" for l in i["notes"]) + "</ul></details>"
    if i.get("notes_url"):
        more += f'<a href="{e(i["notes_url"])}" target="_blank" rel="noopener">{e(i["notes_label"])}</a>'
    more = f'<div class="more">{more}</div>' if more else ""
    return (f'<li class="row{" new" if new else ""}" data-cat="{e(cat)}" data-fav="{1 if i.get("favorite") else 0}" data-recent="{1 if is_recent(i, now) else 0}" data-date="{e(update_date(i))}" data-seen="{e(seen_date(i))}" data-vendor="{e(i.get("vendor") or i["name"])}" data-id="{e(re.sub(r"[^a-z0-9]+", "-", i["name"].lower()).strip("-"))}"><div class="who">{icon_html(i)}<div>{name_html(i)}{badge}{note}</div>{MINE}</div>'
            f'<div class="ver">{num_html(i["version"])}{was}{err}</div>'
            f'<div class="dl">{dl}</div>{more}</li>')

KEEP_UPPER = {"PA", "DJ", "DAW", "IEM", "AV", "RME", "USB", "MIDI"}

def cat_label(c):
    """'PLAYBACK & RECORDING' -> 'Playback & recording' (afkortingen als PA blijven staan)."""
    words = [w if w.upper() in KEEP_UPPER else w.lower() for w in str(c).split()]
    if words and words[0].upper() not in KEEP_UPPER:
        words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)

def render(items, now, contact=None, categories=None, strip=None):
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
    listing = f'{vendors_html(items, strip)}<div class="panel"><ul class="list">{rows}</ul></div><p class="empty" id="empty" hidden></p>'
    if canon:
        nrec = sum(is_recent(i, now) for i in items)
        nav = f'<button type="button" class="rec" data-cat="RECENT" aria-current="false" title="Updates van de afgelopen 30 dagen, nieuwste bovenaan"><span>Nieuwste updates<small>Afgelopen 30 dagen</small></span><b>{nrec}</b></button>'
        nav += '<button type="button" class="my" data-cat="MY" aria-current="false" title="Jouw eigen selectie, bewaard in deze browser"><span>Mijn favorieten<small>Voeg toe met het plusje</small></span><b id="mycount">0</b></button>'
        nav += f'<button type="button" data-cat="*" aria-current="true"><span>Alles</span><b>{len(items)}</b></button>'
        for c in canon.values():
            nav += f'<button type="button" data-cat="{e(c)}" aria-current="false"><span>{e(cat_label(c))}</span><b>{sum(cat_of(i) == c for i in items)}</b></button>'
        layout = f'<div class="layout"><nav class="cats" aria-label="Categorieën">{nav}</nav><div class="main">{listing}</div></div>'
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
                .replace("__SUMMARY__", e(summary)).replace("__BANNER__", banner_svg()).replace("__LOGO__", logo_svg(lit_col="#ffffff", hot_col="#9fd0ff", dim_col="#4b7fac", needle_col="#ffffff")).replace("__MARK__", logo_svg(cls="mark", lit_col="#ffffff", hot_col="#9fd0ff", dim_col="#3d6f9c", needle_col="#ffffff")).replace("__SW__", "".join(f"<i>{ch}</i>" for ch in "SOFTWARE")).replace("__FAVICON__", "data:image/svg+xml," + urllib.parse.quote(logo_svg(tile=True, lit_col="#ffffff", hot_col="#9fd0ff", dim_col="#3d6f9c", needle_col="#ffffff").replace(' class="logo"', '').replace("<svg ", "<svg xmlns='http://www.w3.org/2000/svg' ", 1))).replace("__LAYOUT__", layout).replace("__SEARCH__", search))

import math

def banner_svg():
    """Dunne banner: een rij kanaalstrips van een mengtafel (knop, ledmeter, fader). Vast patroon, zodat de pagina niet bij elke run verandert."""
    W, H = 1440, 56
    p = []
    x0, step, n = 560, 19, 46
    for k in range(n):
        x = x0 + k * step
        lvl = 2 + (k * 7 + (k // 3) * 5) % 6           # 2..7 lampjes aan, onregelmatig maar vast
        fad = ((k * 5 + 3) % 9) / 8                       # faderstand 0..1
        a = math.radians(225 - ((k * 37) % 270))          # knopstand
        p.append(f'<circle cx="{x}" cy="8" r="3.6" fill="none" stroke="#fff" stroke-opacity=".5" stroke-width="1.1"/>')
        p.append(f'<path d="M{x} 8L{x + 3 * math.cos(a):.1f} {8 - 3 * math.sin(a):.1f}" stroke="#fff" stroke-opacity=".8" stroke-width="1.1" stroke-linecap="round"/>')
        for sgm in range(7):
            y = 30 - sgm * 2.9
            col = "#9fd0ff" if sgm >= 5 else "#ffffff"
            p.append(f'<rect x="{x - 3}" y="{y:.1f}" width="6" height="1.9" rx=".4" fill="{col}" fill-opacity="{.85 if sgm < lvl else .12}"/>')
        p.append(f'<path d="M{x} 36V52" stroke="#fff" stroke-opacity=".28" stroke-width="1.6" stroke-linecap="round"/>')
        cy = 50 - fad * 12
        p.append(f'<rect x="{x - 5}" y="{cy - 2:.1f}" width="10" height="4" rx="1" fill="#fff" fill-opacity=".9"/>')
    return (f'<svg class="art" viewBox="0 0 {W} {H}" preserveAspectRatio="xMaxYMid meet" aria-hidden="true">'
            '<defs><linearGradient id="fade" x1="0" x2="1"><stop offset=".38" stop-color="#fff" stop-opacity="0"/><stop offset=".62" stop-color="#fff" stop-opacity=".9"/></linearGradient>'
            f'<mask id="m"><rect width="{W}" height="{H}" fill="url(#fade)"/></mask></defs>'
            f'<g mask="url(#m)">{"".join(p)}</g></svg>')

def logo_svg(tile=False, cls="logo", lit_col="#0b4f8a", hot_col="#2f8fd8", dim_col="#cfd7df", needle_col="#1d2833", tile_col="#0b4f8a"):
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
    bg = f'<rect width="64" height="64" rx="15" fill="{tile_col}"/>' if tile else ""
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
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@125,800&family=Source+Sans+3:wght@300;400;600;700&display=swap" rel="stylesheet">
<style>
:root{color-scheme:light;
--bg:#dfe3e7;--surface:#ffffff;--soft:#f2f4f6;--line:#d8dde2;--ink:#1d2833;--text:#2b3640;--muted:#5d6b77;
--blue:#0b4f8a;--blue-dark:#083a66;--blue-soft:#e8f0f8;--blue-mid:#c9daea}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;min-height:100vh;color:var(--text);font:16px/1.5 "Source Sans 3",system-ui,sans-serif;background:var(--bg)}
.in{max-width:1240px;margin:0 auto;padding-inline:clamp(1rem,3vw,1.5rem)}
.topbar{background:var(--blue);color:#fff}
.topbar .in{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.75rem 1.5rem;padding-block:.9rem}
.brand{display:flex;align-items:baseline;gap:.75rem;color:#fff;text-decoration:none}
.brand{align-items:center}.brand .mark{width:2rem;height:2rem;flex:none}
.brand b{font-family:Archivo,"Source Sans 3",sans-serif;font-stretch:125%;font-weight:800;font-size:1.35rem;letter-spacing:.01em;text-transform:uppercase}
.brand span{font-size:.88rem;color:var(--blue-mid)}
.search{position:relative;flex:0 1 24rem}
.search svg{position:absolute;left:.85rem;top:50%;width:1.05rem;height:1.05rem;transform:translateY(-50%);fill:none;stroke:var(--muted);stroke-width:1.8;stroke-linecap:round;pointer-events:none}
.search input{width:100%;min-height:2.6rem;padding:.5rem .9rem .5rem 2.5rem;border:0;border-radius:4px;background:#fff;color:var(--ink);font:inherit;font-size:1rem}
.search input::placeholder{color:var(--muted)}
.search input:focus-visible{outline:3px solid var(--blue-mid);outline-offset:1px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.hero{position:relative;overflow:hidden;background:linear-gradient(100deg,#062c4f 0%,#0a4478 55%,#0b4f8a 100%);color:#fff;border-top:1px solid rgba(255,255,255,.12)}
.hero .art{position:absolute;inset:0;width:100%;height:100%}
.hero .in{position:relative;display:flex;flex-wrap:wrap;align-items:center;gap:.2rem 1.5rem;min-height:3.4rem;padding-block:.55rem}
.summary{margin:0;font-size:1rem;font-weight:600;color:#fff}
.checked{margin:0;color:var(--blue-mid);font-size:.92rem}
.hero .ask{min-height:2rem;padding:.15rem .8rem;font-size:.86rem}
.wrap{max-width:1240px;margin:0 auto;padding:clamp(1rem,3vw,2rem) clamp(1rem,3vw,1.5rem) 4rem}
.layout{display:flex;flex-wrap:wrap;gap:1.75rem;align-items:flex-start}
.layout>*{min-width:0}
.cats{flex:1 1 15rem;max-width:17rem;position:sticky;top:1rem;display:flex;flex-direction:column;gap:.1rem;padding:.5rem;background:var(--surface);border-radius:6px}
.main{flex:999 1 34rem;display:flex;flex-direction:column;gap:1rem}
.cats button{display:flex;justify-content:space-between;align-items:center;gap:.6rem;width:100%;min-height:2.75rem;padding:.45rem .85rem;border:0;border-radius:4px;background:none;color:var(--text);font:400 1rem/1.25 "Source Sans 3",sans-serif;text-align:left;cursor:pointer;transition:background .15s,color .15s}
.cats button span{text-align:left}
.cats button b{font-weight:400;font-size:.9rem;color:var(--muted)}
.cats button small{display:block;margin-top:.1rem;font-size:.8rem;color:var(--muted)}
.cats button:hover{background:var(--soft)}
.cats button[aria-current=true]{background:var(--blue);color:#fff;font-weight:600}
.cats button[aria-current=true] b,.cats button[aria-current=true] small{color:var(--blue-mid)}
.cats button.my{margin-bottom:.35rem;position:relative}
.cats button.rec{background:#e2f2e7;color:#19663b;margin-bottom:.15rem}
.cats button.rec b,.cats button.rec small{color:#4c8a62}
.cats button.rec:hover{background:#d1e9d9}
.cats button.rec[aria-current=true]{background:#1e7a46;color:#fff}
.cats button.rec[aria-current=true] b,.cats button.rec[aria-current=true] small{color:#cfe9d8}
.cats button.my{background:#e3eef8;color:var(--blue)}
.cats button.my b,.cats button.my small{color:#4d7aa3}
.cats button.my:hover{background:#d4e5f4}
.cats button.my[aria-current=true]{background:#1f6fb2;color:#fff}
.cats button.my[aria-current=true] b,.cats button.my[aria-current=true] small{color:#d6e7f6}
.cats button.my::after{content:"";position:absolute;left:.85rem;right:.85rem;bottom:-.25rem;height:1px;background:var(--line)}
.cats button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.vendors{display:flex;flex-wrap:nowrap;gap:.5rem;padding:.25rem .1rem;overflow-x:auto;scrollbar-width:none}.vendors::-webkit-scrollbar{display:none}
.vendors button{position:relative;display:grid;place-items:center;flex:0 1 2.75rem;min-width:2.2rem;aspect-ratio:1;padding:0;border:1px solid var(--line);border-radius:6px;background:var(--surface);cursor:pointer;transition:border-color .15s,box-shadow .15s}
.vendors button .ico{width:72%;height:72%;border-radius:4px}
.vendors button:hover{border-color:var(--blue)}
.vendors button[aria-pressed=true]{border-color:var(--blue);box-shadow:0 0 0 2px var(--blue)}
.vendors button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.vendors button b{position:absolute;right:-.4rem;top:-.4rem;min-width:1.15rem;height:1.15rem;padding:0 .25rem;border-radius:999px;background:var(--blue);color:#fff;font:600 .68rem/1.15rem "Source Sans 3",sans-serif;text-align:center}
.panel{background:var(--surface);border-radius:6px;padding:.25rem clamp(.75rem,2vw,1.25rem)}
.list{list-style:none;margin:0;padding:0}
.row{position:relative;display:grid;grid-template-columns:minmax(0,1fr) 11rem 13.6rem;gap:.35rem 1.5rem;align-items:center;padding:.8rem .4rem;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row.new{background:linear-gradient(var(--blue-soft),var(--blue-soft)) padding-box;border-radius:4px}
.row.new+.row{border-top-color:transparent}
.who{position:relative;display:flex;align-items:center;gap:.85rem;min-width:0}
.ico{flex:none;width:2.25rem;height:2.25rem;border-radius:6px;object-fit:contain}
img.ico{padding:.25rem;background:#fff;box-shadow:inset 0 0 0 1px var(--line)}
.mono{display:grid;place-items:center;font:700 .74rem/1 "Source Sans 3",sans-serif;letter-spacing:.02em}
.mono.long{font-size:.62rem;letter-spacing:0}
.mono.b{background:var(--soft);color:var(--ink);box-shadow:inset 0 0 0 1px var(--line)}
.mono.r{background:var(--blue-soft);color:var(--blue)}
.name{font-weight:600;font-size:1.02rem;color:var(--ink);text-decoration:none}
a.name{text-decoration:underline;text-decoration-color:transparent;text-underline-offset:.2em;text-decoration-thickness:1.5px;transition:text-decoration-color .15s,color .15s}
a.name:hover,a.name:focus-visible{color:var(--blue);text-decoration-color:currentColor}
.note{display:block;color:var(--muted);font-size:.86rem;margin-top:.05rem}
.badge{display:inline-block;margin-left:.55rem;padding:.05rem .45rem;border-radius:3px;background:var(--blue);color:#fff;font-size:.74rem;font-weight:600;vertical-align:.1em}
.num{display:inline-block;color:var(--blue);font:400 1.25rem/1.2 "Source Sans 3",sans-serif;font-variant-numeric:tabular-nums}
.num.multi{font-size:1rem}.num.multi span{display:block}
.was{display:block;color:var(--muted);font-size:.84rem;margin-top:.05rem}
.err{display:block;color:#a3361f;font-size:.84rem;margin-top:.1rem}
.dl{display:grid;grid-template-columns:repeat(2,6.6rem);gap:.4rem;justify-content:end}
.slot{display:flex;flex-direction:column;gap:.4rem}
.slot .btn{justify-content:center}
.btn{display:inline-flex;align-items:center;gap:.4rem;min-height:2.3rem;padding:.3rem .8rem;border:1px solid var(--blue);border-radius:4px;background:#fff;color:var(--blue);font-size:.9rem;font-weight:600;text-decoration:none;transition:background .15s,color .15s,border-color .15s}
.btn:hover{background:var(--blue);color:#fff}
.btn svg{width:1.05em;height:1.05em;flex:none;position:relative;top:-.08em}
.btn svg.apple{width:.72em;height:.9em;top:-.1em}
.btn.gen{border-color:#8a96a1;color:var(--text)}
.btn.gen:hover{background:var(--text);border-color:var(--text);color:#fff}
.btn:focus-visible,.more summary:focus-visible,.more a:focus-visible,.ask:focus-visible,.mine:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.mine{flex:none;align-self:center;margin-left:auto;display:grid;place-items:center;width:2rem;height:2rem;padding:0;border:1px solid var(--line);border-radius:4px;background:#fff;color:var(--muted);cursor:pointer;transition:border-color .15s,background .15s,color .15s}
.mine svg{width:.95rem;height:.95rem;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.mine .tick{display:none}.mine[aria-pressed=true] .tick{display:block}.mine[aria-pressed=true] .plus{display:none}
.mine:hover{border-color:var(--blue);color:var(--blue)}
.mine[aria-pressed=true]{background:var(--blue);border-color:var(--blue);color:#fff}
.more{grid-column:1/-1;display:flex;flex-wrap:wrap;align-items:flex-start;gap:.3rem 1.5rem;font-size:.9rem;padding-left:3.1rem}
.more summary{cursor:pointer;color:var(--blue);font-weight:600}
.more ul{margin:.35rem 0 0;padding-left:1.1rem;color:var(--text);max-width:75ch}
.more a{color:var(--blue)}
.ask{min-height:2.4rem;padding:.35rem 1rem;border:1px solid var(--blue);border-radius:4px;background:#fff;color:var(--blue);font:inherit;font-size:.92rem;font-weight:600;text-decoration:none;cursor:pointer}
.ask:hover{background:var(--blue);color:#fff}
.hero .ask{background:#fff;border-color:#fff;color:var(--blue)}.hero .ask:hover{background:var(--blue-mid);border-color:var(--blue-mid);color:var(--ink)}
dialog{width:min(30rem,calc(100vw - 2rem));padding:1.25rem;border:0;border-radius:6px;background:#fff;color:var(--text);box-shadow:0 20px 60px -20px rgba(16,32,48,.45)}
dialog::backdrop{background:rgba(20,32,44,.45)}
dialog p{margin:.25rem 0 1rem;color:var(--muted);font-size:.95rem}
dialog label{display:block;margin:0 0 .8rem;color:var(--muted);font-size:.9rem}
dialog textarea,dialog input:not([type=hidden]):not([type=checkbox]){display:block;width:100%;margin-top:.25rem;padding:.5rem .6rem;border:1px solid var(--line);border-radius:4px;background:#fff;color:var(--ink);font:inherit}
dialog textarea{resize:vertical}
dialog textarea:focus-visible,dialog input:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
#cstatus{min-height:1.3em;margin:.25rem 0 .75rem}
.actions{display:flex;gap:.5rem}
button.btn{font-family:inherit;cursor:pointer}
button.btn:disabled{opacity:.6;cursor:default}
.btn.ghost{color:var(--muted);border-color:var(--line)}
.btn.ghost:hover{background:var(--soft);color:var(--ink)}
[hidden]{display:none!important}
.empty{margin:.75rem 0;padding:1.25rem;border:1px dashed var(--line);border-radius:6px;color:var(--muted);background:#fff}
footer{margin-top:2rem;color:var(--muted);font-size:.9rem}
@media (max-width:760px){
.cats{position:static;flex:1 1 100%;max-width:none;flex-direction:row;overflow-x:auto;gap:.4rem;padding:.4rem;scrollbar-width:none}
.cats::-webkit-scrollbar{display:none}
.cats button{width:auto;flex:none;white-space:nowrap;gap:.5rem}
.cats button small{display:none}.cats button.my{margin-bottom:0}.cats button.my::after{display:none}
.row{grid-template-columns:minmax(0,1fr) auto;gap:.5rem .75rem;padding:.75rem .1rem}.who,.more{grid-column:1/-1}.dl{grid-template-columns:repeat(2,6.1rem);gap:.35rem}.was{font-size:.8rem}.more{padding-left:0}
.search{flex:1 1 100%}.hero .art{opacity:.22}.brand{gap:.55rem}.brand .mark{width:1.6rem;height:1.6rem}.brand{flex-wrap:wrap;row-gap:0}.brand b{font-size:.94rem}.brand span{flex-basis:100%;padding-left:2.15rem}.brand b,.brand span{white-space:nowrap}.brand span{font-size:.8rem}}
</style></head><body>
<header class="topbar"><div class="in"><a class="brand" href="./">__MARK__<b>Software Update Watcher</b><span>by evertfrank</span></a>__SEARCH__</div></header>
<h1 class="sr">Software Update Watcher</h1>
<section class="hero">__BANNER__<div class="in"><p class="summary">__SUMMARY__</p><p class="checked">Laatst gecontroleerd __NOW__</p>__ASK__</div></section>
<div class="wrap">
__LAYOUT__
<footer>De lijst wordt elke 6 uur automatisch bijgewerkt.</footer>
</div>
__DIALOG__
<script>
const btns=[...document.querySelectorAll('.cats button')],rows=[...document.querySelectorAll('.row')],list=document.querySelector('.list'),empty=document.getElementById('empty');
let cat='*';
const q=document.getElementById('q');
const vbtns=[...document.querySelectorAll('.vendors button')];
const MYKEY='uw-my-products';let my=new Set();
try{my=new Set(JSON.parse(localStorage.getItem(MYKEY)||'[]'))}catch(_){}
const myCount=document.getElementById('mycount');
function syncMy(){rows.forEach(r=>{const b=r.querySelector('.mine');if(b){const on=my.has(r.dataset.id);b.setAttribute('aria-pressed',String(on));b.title=b.ariaLabel=on?'Verwijderen uit Mijn favorieten':'Toevoegen aan Mijn favorieten'}});if(myCount)myCount.textContent=[...rows].filter(r=>my.has(r.dataset.id)).length}
rows.forEach(r=>{const b=r.querySelector('.mine');if(b)b.addEventListener('click',()=>{const id=r.dataset.id;my.has(id)?my.delete(id):my.add(id);try{localStorage.setItem(MYKEY,JSON.stringify([...my]))}catch(_){}syncMy();if(cat==='MY')show()})});
const byDate=[...rows].sort((a,b)=>{const ra=a.dataset.date||'',rb=b.dataset.date||'';if(!!ra!==!!rb)return ra?-1:1;return rb.localeCompare(ra)||(b.dataset.seen||'').localeCompare(a.dataset.seen||'')});   // releasedatum, nieuwste eerst; zonder releasedatum onderaan
function show(c){
  if(c!==undefined)cat=c;
  const term=q.value.trim().toLowerCase(),eff=term?'*':cat;   // zoeken doet altijd ALL
  let n=0;
  rows.forEach(r=>{const on=(eff==='*'||(eff.startsWith('V:')?r.dataset.vendor===eff.slice(2):eff==='MY'?my.has(r.dataset.id):eff==='RECENT'?r.dataset.recent==='1':r.dataset.cat===eff))&&(!term||r.textContent.toLowerCase().includes(term));r.hidden=!on;if(on)n++});
  (eff==='RECENT'?byDate:rows).forEach(r=>list.appendChild(r));
  btns.forEach(b=>b.setAttribute('aria-current',String(b.dataset.cat===eff)));
  vbtns.forEach(b=>b.setAttribute('aria-pressed',String('V:'+b.dataset.v===eff)));
  list.hidden=n===0;empty.hidden=n!==0;
  if(!n)empty.textContent=term?'Niets gevonden voor "'+q.value.trim()+'".':(cat==='RECENT'?'Geen updates in de afgelopen 30 dagen.':cat==='MY'?'Nog niets geselecteerd. Klik bij een programma op het plusje om het aan Mijn favorieten toe te voegen.':'Hier staat nog niets. Zet category: '+cat+' bij een programma in software.yml.');
}
btns.forEach(b=>b.addEventListener('click',()=>{q.value='';show(b.dataset.cat);history.replaceState(null,'','#'+(b.dataset.cat==='*'?'ALL':encodeURIComponent(b.dataset.cat)))}));
vbtns.forEach(b=>b.addEventListener('click',()=>{const c='V:'+b.dataset.v;q.value='';const nc=cat===c?'*':c;show(nc);history.replaceState(null,'','#'+(nc==='*'?'ALL':encodeURIComponent(nc)))}));
q.addEventListener('input',()=>show());
document.addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==q&&!/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)){e.preventDefault();q.focus()}else if(e.key==='Escape'&&document.activeElement===q){q.value='';show();q.blur()}});
syncMy();
{const h=decodeURIComponent(location.hash.slice(1));show(h==='ALL'?'*':btns.some(b=>b.dataset.cat===h)||vbtns.some(b=>'V:'+b.dataset.v===h)?h:'RECENT')}
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
