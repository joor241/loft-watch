"""Wacht tot The Loft (ADE) een event toevoegt en stuur dan meteen een pushmelding.

Twee bronnen, elke POLL_SECONDS:
  1. Fourvenues (de ticketshop achter theloftamsterdam.com): de publieke API die
     hun eigen widget gebruikt. De client-token staat in hun JS-bundle en wordt
     daar bij het starten (en na een 401/403) automatisch uitgehaald.
  2. WordPress REST-API van theloftamsterdam.com: een event-post krijgt als
     publicatiedatum de starttijd (bv. 2026-10-24T14:30).

Melden:
  - DOEL   (naam bevat een TARGET_NAMES-woord, of start op TARGET_DATE binnen
            TARGET_WINDOW_MIN minuten van TARGET_TIME): urgente push.
  - Overig nieuw event: gewone push (kan een geheime/vervangende naam zijn).
  - Fouten: een bron die ERROR_ALERT_AFTER_MIN blijft falen (blokkade, rate-limit,
    API veranderd, lege lijst, ...) geeft een push met uitleg, en een push als
    hij weer werkt. Falen beide bronnen tegelijk, dan is die push urgent.

Twee rollen in GitHub Actions, die elkaar via een "hartslag" bewaken:
  - watch  (python loft_watch.py)          : scant elke 20s.
  - guard  (python loft_watch.py --guard)  : controleert elke 30s of de scanner
            nog hartslag geeft. Blijft die STALE_ALERT_MIN weg, dan: push, de
            scanner herstarten en zelf doorscannen tot hij terug is.
  De scanner bewaakt op zijn beurt de bewaker. De hartslag is een commit-status
  op de eerste commit (HB_SHA) - een plek waar GITHUB_TOKEN mag schrijven en
  die de ander live kan lezen.
  Daarnaast: watchdog-thread tegen hangen, elke dag om DAILY_STATUS_HOUR een
  "ik draai nog"-push (blijft die weg, dan ligt alles plat), en mislukte
  pushes worden opnieuw geprobeerd en zo nodig als GitHub-issue gemeld.

Instellingen via omgevingsvariabelen LOFT_<naam> (zie DEFAULTS); de ntfy-topic
wordt lokaal bij de eerste start willekeurig gekozen en in config.json bewaard.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

ON_WINDOWS = sys.platform == "win32"
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
STATE_FILE = os.path.join(HERE, "seen.json")
LOG_FILE = os.path.join(HERE, "loft_watch.log")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

DEFAULTS = {
    "POLL_SECONDS": "20",
    "TARGET_NAMES": "mau p,dennis cruz",
    "TARGET_DATE": "2026-10-24",
    "TARGET_TIME": "14:30",
    "TARGET_WINDOW_MIN": "90",
    "FV_SLUG": "the-loft",
    "WP_SITE": "https://theloftamsterdam.com",
    "NTFY_SERVER": "https://ntfy.sh",
    "OPEN_BROWSER": "1",
    "MAX_RUNTIME_MIN": "0",  # 0 = onbeperkt
    "COMMIT_STATE": "0",
    "ERROR_ALERT_AFTER_MIN": "3",   # zo lang moet een bron falen voor er een push komt
    "ERROR_REMIND_MIN": "60",       # herinnering zolang het blijft falen
    "STALE_ALERT_MIN": "4",         # zo lang mag de hartslag van de ander wegblijven
    "HANG_MIN": "3",                # geen scan voltooid in deze tijd = proces hangt
    "DAILY_STATUS_HOUR": "12",      # "ik draai nog"-push; leeg = uit
    "HB_SHA": "1155bee3b52357023b240c2ba34d83b050507c89",
}

PRIO = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def setting(name):
    return os.environ.get("LOFT_" + name, DEFAULTS[name])


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def dur(seconds):
    m = int(seconds // 60)
    return f"{m} min" if m < 60 else f"{m // 60}u{m % 60:02d}"


class SourceError(Exception):
    """Fout met een al begrijpelijke uitleg."""


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def get_json(url, headers=None):
    text = http_get(url, headers)
    if text.lstrip()[:1] == "<":
        raise SourceError("kreeg een webpagina in plaats van data: waarschijnlijk een blokkade, "
                          "captcha (Cloudflare) of wachtrij (Queue-Fair)")
    return json.loads(text)


def describe(e, host):
    """Maak van een exceptie een uitleg die je op je telefoon snapt."""
    if isinstance(e, urllib.error.HTTPError):
        c = e.code
        if c == 403:
            return f"HTTP 403 geweigerd: {host} blokkeert waarschijnlijk de GitHub-servers (bot-detectie of IP-blokkade)"
        if c == 429:
            return f"HTTP 429: te veel verzoeken, {host} remt ons af (rate-limit); scanner wacht en probeert opnieuw"
        if c == 401:
            return "HTTP 401: toegangssleutel geweigerd, ook na opnieuw ophalen"
        if c >= 500:
            return f"HTTP {c}: storing bij {host} zelf"
        return f"HTTP {c}: de API van {host} is veranderd; script moet aangepast"
    if isinstance(e, SourceError):
        return str(e)
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return f"geen verbinding met {host} ({getattr(e, 'reason', e)})"
    if isinstance(e, (ValueError, KeyError, TypeError, AttributeError, IndexError)):
        return (f"onverwacht antwoord van {host} ({type(e).__name__}: {e}); "
                "site/API is waarschijnlijk veranderd, script moet aangepast")
    return f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- GitHub (hartslag, herstarten, issues)

class GitHub:
    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN")
        self.repo = os.environ.get("GITHUB_REPOSITORY")
        self.run_id = os.environ.get("GITHUB_RUN_ID")
        self.enabled = bool(self.token and self.repo)

    def api(self, method, path, body=None):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "User-Agent": "loft-watch"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None

    def heartbeat(self, role, desc):
        # Per uur een nieuwe context: GitHub staat max. 1000 statussen per sha+context toe.
        ctx = f"heartbeat/{role}/{datetime.now(timezone.utc):%Y%m%d%H}"
        self.api("POST", f"/statuses/{setting('HB_SHA')}",
                 {"state": "success", "context": ctx, "description": desc[:140]})

    def last_heartbeat(self, role, prefix="ok"):
        """(timestamp, beschrijving) van de laatste hartslag van `role`, of (None, None)."""
        for s in self.api("GET", f"/commits/{setting('HB_SHA')}/statuses?per_page=100"):
            if s["context"].startswith(f"heartbeat/{role}/") and s["description"].startswith(prefix):
                ts = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")).timestamp()
                return ts, s["description"]
        return None, None

    def active_runs(self, workflow):
        runs = self.api("GET", f"/actions/workflows/{workflow}/runs?per_page=10")["workflow_runs"]
        return [r for r in runs if r["status"] != "completed" and str(r["id"]) != str(self.run_id)]

    def ensure_running(self, workflow):
        """Start `workflow` als er (behalve deze run) geen run actief of in de wachtrij staat."""
        if self.active_runs(workflow):
            return False
        self.api("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": "main"})
        log(f"{workflow} gestart")
        return True

    def issue(self, title, body):
        self.api("POST", "/issues", {"title": title[:200], "body": body})


def safe(fn, *args, what="", **kw):
    try:
        return fn(*args, **kw)
    except Exception as e:
        log(f"{what or fn.__name__} mislukt: {e!r}")
        return None


# ---------------------------------------------------------------- pushmeldingen

class Notifier:
    """Verstuurt in een eigen thread, zodat een trage/onbereikbare ntfy het scannen
    nooit ophoudt. Mislukte pushes blijven in de wachtrij (urgentste eerst) en worden
    met oplopende pauze opnieuw geprobeerd; belangrijke die >5 min blijven hangen
    gaan daarnaast als GitHub-issue de deur uit (mail/GitHub-app)."""

    def __init__(self, topic, gh):
        self.topic, self.gh = topic, gh
        self.queue = []
        self.cv = threading.Condition()
        threading.Thread(target=self._worker, daemon=True).start()

    def push(self, title, body, url=None, prio="default", tags=""):
        log(f"PUSH[{prio}] {title} | {body.splitlines()[0] if body else ''}")
        msg = {"title": title, "body": body, "url": url, "prio": prio, "tags": tags,
               "t0": time.time(), "tries": 0, "next": 0, "issued": False}
        with self.cv:
            self.queue = (self.queue + [msg])[-50:]
            self.cv.notify()

    def drain(self, timeout):
        end = time.time() + timeout
        with self.cv:
            while self.queue and time.time() < end:
                self.cv.wait(timeout=1)

    def _send(self, m):
        headers = {"Title": m["title"].encode("utf-8"), "Priority": m["prio"]}
        if m["tags"]:
            headers["Tags"] = m["tags"]
        if m["url"]:
            headers["Click"] = m["url"]
            headers["Actions"] = f"view, Openen, {m['url']}"
        req = urllib.request.Request(f"{setting('NTFY_SERVER')}/{self.topic}",
                                     data=m["body"].encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10).read()

    def _worker(self):
        while True:
            with self.cv:
                while True:
                    now = time.time()
                    due = [m for m in self.queue if m["next"] <= now]
                    if due:
                        break
                    self.cv.wait(timeout=min((m["next"] for m in self.queue), default=now + 60) - now)
                m = max(due, key=lambda x: (PRIO[x["prio"]], -x["t0"]))
            try:
                self._send(m)
                ok = True
            except Exception as e:
                ok = False
                if m["tries"] in (0, 3) or m["tries"] % 20 == 0:
                    log(f"ntfy-push mislukt ({m['tries'] + 1}x): {e!r}")
            with self.cv:
                if ok:
                    if m in self.queue:
                        self.queue.remove(m)
                    if m["tries"]:
                        log(f"push alsnog verstuurd na {m['tries']} pogingen: {m['title']}")
                else:
                    m["tries"] += 1
                    m["next"] = time.time() + min(5 * 2 ** m["tries"], 60)
                self.cv.notify_all()
            if (not ok and PRIO[m["prio"]] >= PRIO["high"] and not m["issued"] and self.gh.enabled
                    and time.time() - m["t0"] > 300):
                if safe(self.gh.issue, "[loft-watch] " + m["title"],
                        f"ntfy.sh is onbereikbaar, daarom komt deze melding via GitHub.\n\n"
                        f"{m['body']}\n\n{m['url'] or ''}", what="GitHub-issue als noodmelding") is not None:
                    m["issued"] = True


# ---------------------------------------------------------------- Fourvenues

class Fourvenues:
    API = "https://cli-api-service.fourvenues.com/api"
    SITE = "https://site.fourvenues.com"

    def __init__(self, slug):
        self.slug = slug
        self.headers = None

    def _refresh_token(self):
        shell = http_get(f"{self.SITE}/en/iframe/{self.slug}/events")
        m = re.search(r'src="(main\.[0-9a-f]+\.js)"', shell)
        if not m:
            raise SourceError("toegangssleutel niet te vinden op site.fourvenues.com: "
                              "waarschijnlijk wachtrij (Queue-Fair), blokkade of de site is omgebouwd")
        js = http_get(f"{self.SITE}/{m.group(1)}")
        tok = re.search(r'cliApiServiceToken="([^"]+)"', js)
        app = re.search(r'connectorAppId="([^"]+)"', js)
        if not (tok and app):
            raise SourceError("toegangssleutel niet meer in de JS van Fourvenues: site is omgebouwd, script moet aangepast")
        self.headers = {"Authorization": "Fv " + tok.group(1), "x-application": app.group(1),
                        "Accept-Language": "en"}

    def events(self):
        if self.headers is None:
            self._refresh_token()
        now = int(time.time())
        q = urllib.parse.urlencode({"slug": self.slug, "startDate": now - 86400,
                                    "endDate": now + 360 * 86400, "pageSize": 100, "page": 1})  # max 1 jaar
        try:
            data = get_json(f"{self.API}/events?{q}", self.headers)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            self._refresh_token()  # token geroteerd: opnieuw uit de bundle halen
            data = get_json(f"{self.API}/events?{q}", self.headers)
        out = []
        for ev in data["data"]:
            out.append({
                "key": "fv:" + ev["id"],
                "name": ev["name"],
                "start": datetime.fromtimestamp(ev["dates"]["start"]),  # lokale (NL) tijd; Actions: TZ=Europe/Amsterdam
                "url": f"{self.SITE}/en/{self.slug}/events/{ev['slug']}-{ev['code']}",
                "extra": " ".join(a.get("name", "") for a in ev.get("artists") or []),
            })
        return out


# ---------------------------------------------------------------- WordPress

def wp_events(site):
    q = urllib.parse.urlencode({"per_page": 50, "_fields": "id,date,link,title", "orderby": "date"})
    out = []
    for p in get_json(f"{site}/wp-json/wp/v2/posts?{q}"):
        out.append({
            "key": f"wp:{p['id']}",
            "name": re.sub(r"<[^>]+>|&#?\w+;", " ", p["title"]["rendered"]).strip(),
            "start": datetime.fromisoformat(p["date"]),
            "url": p["link"],
            "extra": "",
        })
    return out


# ---------------------------------------------------------------- matching

def is_target(ev):
    hay = (ev["name"] + " " + ev["extra"]).lower()
    hay_compact = re.sub(r"[^a-z0-9]", "", hay)
    for n in setting("TARGET_NAMES").split(","):
        n = n.strip().lower()
        if n and (n in hay or re.sub(r"[^a-z0-9]", "", n) in hay_compact):
            return True
    target = datetime.fromisoformat(f"{setting('TARGET_DATE')}T{setting('TARGET_TIME')}")
    return abs(ev["start"] - target) <= timedelta(minutes=int(setting("TARGET_WINDOW_MIN")))


def popup(title, body):
    import ctypes
    import winsound

    def run():
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.4)
        ctypes.windll.user32.MessageBoxW(0, body, title, 0x40 | 0x40000)  # info + topmost
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- bron-gezondheid

class Health:
    def __init__(self, label, host):
        self.label, self.host = label, host
        self.down_since = None
        self.alerted_at = None
        self.err = ""
        self.last_count = None
        self.skip_until = 0

    def ok(self, n, notify):
        if self.alerted_at:
            notify.push(f"✅ {self.label} werkt weer",
                        f"Lag {dur(time.time() - self.down_since)} plat ({self.err}). Scannen loopt weer.",
                        prio="default", tags="white_check_mark")
        elif self.down_since:
            log(f"{self.label} weer ok na {dur(time.time() - self.down_since)}")
        self.down_since = self.alerted_at = None
        self.last_count = n

    def fail(self, e):
        self.err = describe(e, self.host)
        if self.down_since is None:
            self.down_since = time.time()
            log(f"{self.label} faalt: {self.err}")
        if isinstance(e, urllib.error.HTTPError) and e.code == 429:
            try:
                wait = int(e.headers.get("Retry-After") or 120)
            except ValueError:
                wait = 120
            self.skip_until = time.time() + min(max(wait, 30), 900)

    def maybe_alert(self, notify, all_down):
        if self.down_since is None:
            return
        now = time.time()
        if now - self.down_since < float(setting("ERROR_ALERT_AFTER_MIN")) * 60:
            return
        if self.alerted_at and now - self.alerted_at < float(setting("ERROR_REMIND_MIN")) * 60:
            return
        self.alerted_at = now
        if all_down:
            impact = ("LET OP: beide bronnen falen, je krijgt nu GEEN melding als het event online komt. "
                      "Check zelf: https://site.fourvenues.com/en/the-loft/events")
        else:
            impact = "De andere bron werkt nog, dus een nieuw event wordt waarschijnlijk wel gemeld."
        notify.push(f"⚠️ {self.label} faalt al {dur(now - self.down_since)}",
                    f"{self.err}\n{impact}", url="https://site.fourvenues.com/en/the-loft/events",
                    prio="urgent" if all_down else "high", tags="warning")


# ---------------------------------------------------------------- scannen

class Scanner:
    def __init__(self, notify):
        self.notify = notify
        state = load_json(STATE_FILE, {})
        self.seen = set(state.get("seen", []))
        self.baselined = set(state.get("baselined", []))  # bronnen waarvan de bestaande events al vastliggen
        fv = Fourvenues(setting("FV_SLUG"))
        self.sources = [
            ("fourvenues", fv.events, Health("Fourvenues (tickets)", "fourvenues.com")),
            ("wordpress", lambda: wp_events(setting("WP_SITE")), Health("Website The Loft", "theloftamsterdam.com")),
        ]
        self.scans = 0
        self.last_scan = None
        self.max_gap = 0.0

    def alert_event(self, ev, urgent):
        when = ev["start"].strftime("%a %d %b %H:%M")
        title = ("🚨 THE LOFT: " if urgent else "The Loft nieuw event: ") + ev["name"]
        body = f"{when}\n{ev['url']}"
        self.notify.push(title, body, url=ev["url"], prio="urgent" if urgent else "default",
                         tags="rotating_light,ticket" if urgent else "calendar")
        if urgent and ON_WINDOWS:
            popup(title, body)
            if setting("OPEN_BROWSER") == "1":
                webbrowser.open(ev["url"])

    def cycle(self):
        before = len(self.seen)
        for name, fetch, h in self.sources:
            if time.time() < h.skip_until:
                continue
            try:
                events = fetch()
                if not events and h.last_count:
                    raise SourceError(f"lege lijst teruggekregen (eerder {h.last_count} events): "
                                      "mogelijk blokkade of de API is veranderd")
            except Exception as e:
                h.fail(e)
                continue
            h.ok(len(events), self.notify)
            for ev in events:
                if ev["key"] in self.seen:
                    continue
                self.seen.add(ev["key"])
                if name not in self.baselined and not is_target(ev):
                    continue  # bestaande events bij de eerste geslaagde ophaalronde alleen onthouden
                self.alert_event(ev, urgent=is_target(ev))
            if name not in self.baselined:
                self.baselined.add(name)
                log(f"{name}: basislijn vastgelegd ({len(events)} bestaande events)")
        save_json(STATE_FILE, {"seen": sorted(self.seen), "baselined": sorted(self.baselined)})
        if len(self.seen) != before and setting("COMMIT_STATE") == "1":
            commit_state(f"loft-watch: {len(self.seen) - before} nieuw(e) event(s)")

        down = [h for _, _, h in self.sources if h.down_since]
        for h in down:
            h.maybe_alert(self.notify, all_down=len(down) == len(self.sources))
        now = time.time()
        if self.last_scan:
            self.max_gap = max(self.max_gap, now - self.last_scan)
        self.last_scan = now
        self.scans += 1

    def summary(self):
        return " ".join(f"{n}={'ERR' if h.down_since else h.last_count}" for n, _, h in self.sources)


def commit_state(msg):
    """Alleen in GitHub Actions: seen.json terugzetten in de repo."""
    try:
        subprocess.run(["git", "add", "seen.json"], cwd=HERE, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", msg], cwd=HERE, check=True, capture_output=True)
        subprocess.run(["git", "pull", "--rebase", "-X", "theirs", "-q"], cwd=HERE, capture_output=True)
        subprocess.run(["git", "push", "-q"], cwd=HERE, check=True, capture_output=True)
    except Exception as e:
        log(f"state committen mislukt: {e!r}")


class Watchdog(threading.Thread):
    """Voltooit de hoofdlus HANG_MIN lang geen ronde, dan het proces hard stoppen;
    de workflow start daarna een verse run (en de bewaker merkt het gat)."""

    def __init__(self, role, notify):
        super().__init__(daemon=True)
        self.role, self.notify = role, notify
        self.beat = time.time()

    def kick(self):
        self.beat = time.time()

    def run(self):
        limit = float(setting("HANG_MIN")) * 60
        while True:
            time.sleep(15)
            if time.time() - self.beat > limit:
                log(f"{self.role} hangt al {dur(time.time() - self.beat)}, proces wordt herstart")
                if not IN_ACTIONS:  # in Actions meldt de bewaker het gat al
                    safe(self.notify.push, f"⚠️ loft-watch hangt", "Geen scan meer voltooid; herstart.", prio="high")
                os._exit(3)


def sleep_until(t):
    time.sleep(max(1.0, t - time.time()))


# ---------------------------------------------------------------- rol: scanner

def run_watch(notify, gh):
    scanner = Scanner(notify)
    wd = Watchdog("scanner", notify)
    wd.start()
    t0 = time.time()
    max_min = int(setting("MAX_RUNTIME_MIN"))
    stop_at = t0 + max_min * 60 if max_min else None
    stale = float(setting("STALE_ALERT_MIN")) * 60
    poll = int(setting("POLL_SECONDS"))
    last_hb = last_peer = guard_alerted = 0
    successor_done = daily_sent = False

    if gh.enabled:  # gat sinds de vorige run? (alleen melden als de bewaker het niet al deed)
        prev, _ = safe(gh.last_heartbeat, "watch", what="vorige hartslag lezen") or (None, None)
        g_ts, _ = safe(gh.last_heartbeat, "guard", what="hartslag bewaker lezen") or (None, None)
        guard_alive = g_ts and t0 - g_ts < 120
        if prev and t0 - prev > stale and not guard_alive:
            notify.push(f"⚠️ Gat van {dur(t0 - prev)} zonder scans",
                        f"Er is niet gescand tussen {hhmm(prev)} en {hhmm(t0)}, en de bewaker lag ook stil. "
                        "Scanner loopt nu weer. Check voor de zekerheid zelf even de agenda.",
                        url="https://site.fourvenues.com/en/the-loft/events", prio="high", tags="warning")

    while stop_at is None or time.time() < stop_at:
        started = time.time()
        scanner.cycle()
        wd.kick()
        now = time.time()

        if gh.enabled and now - last_hb >= 60:
            safe(gh.heartbeat, "watch", "ok " + scanner.summary(), what="hartslag")
            last_hb = now
        if gh.enabled and now - last_peer >= 60 and now - t0 > 300:
            last_peer = now
            g_ts, _ = safe(gh.last_heartbeat, "guard", what="hartslag bewaker lezen") or (None, None)
            if g_ts is None or now - g_ts > 2 * stale:
                restarted = safe(gh.ensure_running, "guard.yml", what="bewaker herstarten")
                if now - guard_alerted > 3600:
                    guard_alerted = now
                    notify.push("Bewaker lag stil" + (", herstart" if restarted else ""),
                                f"Laatste hartslag bewaker: {hhmm(g_ts) if g_ts else 'onbekend'}. "
                                "Scannen zelf loopt gewoon door.", prio="default", tags="shield")

        hour = setting("DAILY_STATUS_HOUR")
        lt = datetime.now()
        if hour and not daily_sent and lt.hour == int(hour) and lt.minute < 15:
            daily_sent = True
            notify.push("✅ Loft-watch draait",
                        f"Scant elke {poll}s. Deze run: {scanner.scans} scans sinds {hhmm(t0)}, "
                        f"langste gat {int(scanner.max_gap)}s. Stand: {scanner.summary()}. "
                        "Krijg je deze melding een dag niet, dan ligt alles plat.",
                        prio="low", tags="white_check_mark")

        if gh.enabled and stop_at and not successor_done and stop_at - now < 600:
            successor_done = True  # opvolger alvast in de wachtrij: start zodra deze run stopt
            safe(gh.ensure_running, "watch.yml", what="opvolger starten")
        sleep_until(started + poll)

    if gh.enabled:
        safe(gh.heartbeat, "watch", "ok overdracht " + scanner.summary(), what="hartslag")
    log("maximale looptijd bereikt, stop (volgende run neemt het over)")


# ---------------------------------------------------------------- rol: bewaker

def run_guard(notify, gh):
    if not gh.enabled:
        sys.exit("--guard werkt alleen in GitHub Actions (GITHUB_TOKEN/GITHUB_REPOSITORY ontbreken)")
    wd = Watchdog("bewaker", notify)
    wd.start()
    t0 = time.time()
    max_min = int(setting("MAX_RUNTIME_MIN"))
    stop_at = t0 + max_min * 60 if max_min else None
    stale = float(setting("STALE_ALERT_MIN")) * 60
    poll = int(setting("POLL_SECONDS"))
    last_hb = last_restart = alerted_at = 0
    down_since = None
    backup = None  # Scanner zolang de bewaker het scannen overneemt
    successor_done = False

    while stop_at is None or time.time() < stop_at:
        started = time.time()
        w_ts = "?"
        try:
            w_ts, _ = gh.last_heartbeat("watch")
        except Exception as e:
            log(f"hartslag scanner lezen mislukt: {e!r}")  # onbekend: niets concluderen
        now = time.time()

        if w_ts != "?":
            dead = (w_ts is None and now - t0 > stale) or (w_ts is not None and now - w_ts > stale)
            if dead:
                if down_since is None:
                    down_since = w_ts or t0
                    log(f"scanner stil sinds {hhmm(down_since)}")
                if now - last_restart >= 120:
                    last_restart = now
                    safe(gh.ensure_running, "watch.yml", what="scanner herstarten")
                if not alerted_at or now - alerted_at >= 15 * 60:
                    alerted_at = now
                    notify.push(f"⚠️ Scanner ligt stil sinds {hhmm(down_since)}",
                                f"Al {dur(now - down_since)} geen hartslag. De bewaker scant nu zelf door "
                                "en probeert de scanner te herstarten.", prio="high", tags="warning")
                if backup is None:
                    if IN_ACTIONS:  # nieuwste seen.json, zodat al gemelde events niet opnieuw komen
                        subprocess.run("git fetch -q origin main && git reset -q --hard origin/main",
                                       shell=True, cwd=HERE)
                    backup = Scanner(notify)
                    log("bewaker neemt het scannen over")
                backup.cycle()
            elif down_since is not None:
                if alerted_at:
                    notify.push("✅ Scanner loopt weer",
                                f"Was {dur(now - down_since)} stil (vanaf {hhmm(down_since)}); "
                                "de bewaker heeft in de tussentijd gescand.", prio="default", tags="white_check_mark")
                log("scanner weer actief")
                down_since, alerted_at, backup = None, 0, None

        wd.kick()
        if now - last_hb >= 60:
            safe(gh.heartbeat, "guard", "ok" + (" (neemt scannen over)" if backup else ""), what="hartslag")
            last_hb = now
        if stop_at and not successor_done and stop_at - now < 600:
            successor_done = True
            safe(gh.ensure_running, "guard.yml", what="opvolger starten")
        sleep_until(started + (poll if backup else 30))

    safe(gh.heartbeat, "guard", "ok overdracht", what="hartslag")
    log("maximale looptijd bereikt, stop (volgende run neemt het over)")


# ---------------------------------------------------------------- main

def crash_report(notify, gh, role, e):
    kind = f"CRASH {type(e).__name__}"
    repeat = False
    if gh.enabled:
        ts, desc = safe(gh.last_heartbeat, role, prefix="CRASH", what="crash-check") or (None, None)
        repeat = bool(desc and desc.startswith(kind) and time.time() - ts < 2 * 3600)
        safe(gh.heartbeat, role, f"{kind}: {e}", what="crash-hartslag")
    if not repeat:  # bij een crash-lus maar één melding
        notify.push(f"💥 loft-watch ({role}) gecrasht",
                    f"{type(e).__name__}: {e}\nHerstart gaat automatisch. Blijft dit gebeuren, dan moet "
                    "het script aangepast worden.", prio="high", tags="boom")
    notify.drain(30)
    if IN_ACTIONS:
        time.sleep(60)  # crash-lus afremmen


def main():
    gh = GitHub()
    if "--ensure-successor" in sys.argv:  # laatste workflow-stap: opvolger garanderen
        wf = sys.argv[sys.argv.index("--ensure-successor") + 1]
        if gh.enabled:
            safe(gh.ensure_running, wf, what="opvolger starten")
        return

    cfg = load_json(CONFIG_FILE, {})
    topic = os.environ.get("LOFT_NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic:
        if IN_ACTIONS:
            msg = "NTFY_TOPIC-secret ontbreekt of is leeg: er kunnen geen pushmeldingen verstuurd worden."
            safe(gh.issue, "[loft-watch] NTFY_TOPIC ontbreekt", msg, what="issue")
            sys.exit(msg)
        topic = cfg["ntfy_topic"] = "loft-ade-" + secrets.token_hex(6)
        save_json(CONFIG_FILE, cfg)
    notify = Notifier(topic, gh)

    if "--test" in sys.argv:
        Scanner(notify).alert_event({"name": "TEST Mau P & Dennis Cruz", "start": datetime.now(),
                                     "url": "https://theloftamsterdam.com/", "extra": ""}, urgent=True)
        notify.drain(30)
        time.sleep(3)
        return

    role = "guard" if "--guard" in sys.argv else "watch"
    shown_topic = topic if not IN_ACTIONS else topic[:6] + "…"  # niet volledig in openbare Actions-logs
    log(f"start {role} | ntfy-topic: {shown_topic} | doel: {setting('TARGET_NAMES')} / "
        f"{setting('TARGET_DATE')} {setting('TARGET_TIME')} | elke {setting('POLL_SECONDS')}s")
    try:
        (run_guard if role == "guard" else run_watch)(notify, gh)
    except Exception as e:
        log(traceback.format_exc())
        crash_report(notify, gh, role, e)
        raise


if __name__ == "__main__":
    main()
