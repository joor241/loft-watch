"""Wacht tot The Loft (ADE) een event toevoegt en stuur dan meteen een pushmelding.

Twee bronnen, elke POLL_SECONDS:
  1. Fourvenues (de ticketshop achter theloftamsterdam.com): de publieke API die
     hun eigen widget gebruikt. De client-token staat in hun JS-bundle en wordt
     daar bij het starten (en na een 401) automatisch uitgehaald.
  2. WordPress REST-API van theloftamsterdam.com: een event-post krijgt als
     publicatiedatum de starttijd (bv. 2026-10-24T14:30).

Melden:
  - DOEL   (naam bevat een TARGET_NAMES-woord, of start op TARGET_DATE binnen
            TARGET_WINDOW_MIN minuten van TARGET_TIME): urgente push + popup +
            ticketpagina openen.
  - Overig nieuw event: gewone push (kan een geheime/vervangende naam zijn).

Instellingen via omgevingsvariabelen (zie DEFAULTS); de ntfy-topic wordt bij de
eerste start willekeurig gekozen en in config.json bewaard.

Draait ook headless (GitHub Actions): popup/browser vallen dan weg,
MAX_RUNTIME_MIN laat de run netjes stoppen en COMMIT_STATE=1 pusht seen.json
na elke melding terug naar de repo, zodat de volgende run niet opnieuw meldt.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta

ON_WINDOWS = sys.platform == "win32"

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
}


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


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------- Fourvenues

class Fourvenues:
    API = "https://cli-api-service.fourvenues.com/api"
    SITE = "https://site.fourvenues.com"

    def __init__(self, slug):
        self.slug = slug
        self.headers = None

    def _refresh_token(self):
        shell = http_get(f"{self.SITE}/en/iframe/{self.slug}/events")
        main = re.search(r'src="(main\.[0-9a-f]+\.js)"', shell).group(1)
        js = http_get(f"{self.SITE}/{main}")
        self.headers = {
            "Authorization": "Fv " + re.search(r'cliApiServiceToken="([^"]+)"', js).group(1),
            "x-application": re.search(r'connectorAppId="([^"]+)"', js).group(1),
            "Accept-Language": "en",
        }

    def events(self):
        if self.headers is None:
            self._refresh_token()
        now = int(time.time())
        q = urllib.parse.urlencode({"slug": self.slug, "startDate": now - 86400,
                                    "endDate": now + 360 * 86400, "pageSize": 100, "page": 1})  # max 1 jaar
        try:
            data = json.loads(http_get(f"{self.API}/events?{q}", self.headers))
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            self._refresh_token()  # token geroteerd: opnieuw uit de bundle halen
            data = json.loads(http_get(f"{self.API}/events?{q}", self.headers))
        out = []
        for ev in data["data"]:
            start = datetime.fromtimestamp(ev["dates"]["start"])  # lokale (NL) tijd van deze pc
            out.append({
                "key": "fv:" + ev["id"],
                "name": ev["name"],
                "start": start,
                "url": f"{self.SITE}/en/{self.slug}/events/{ev['slug']}-{ev['code']}",
                "extra": " ".join(a.get("name", "") for a in ev.get("artists") or []),
            })
        return out


# ---------------------------------------------------------------- WordPress

def wp_events(site):
    q = urllib.parse.urlencode({"per_page": 50, "_fields": "id,date,link,title", "orderby": "date"})
    out = []
    for p in json.loads(http_get(f"{site}/wp-json/wp/v2/posts?{q}")):
        out.append({
            "key": f"wp:{p['id']}",
            "name": re.sub(r"<[^>]+>|&#?\w+;", " ", p["title"]["rendered"]).strip(),
            "start": datetime.fromisoformat(p["date"]),
            "url": p["link"],
            "extra": "",
        })
    return out


# ---------------------------------------------------------------- matching / alerts

def is_target(ev):
    hay = (ev["name"] + " " + ev["extra"]).lower()
    hay_compact = re.sub(r"[^a-z0-9]", "", hay)
    for n in setting("TARGET_NAMES").split(","):
        n = n.strip().lower()
        if n and (n in hay or re.sub(r"[^a-z0-9]", "", n) in hay_compact):
            return True
    target = datetime.fromisoformat(f"{setting('TARGET_DATE')}T{setting('TARGET_TIME')}")
    return abs(ev["start"] - target) <= timedelta(minutes=int(setting("TARGET_WINDOW_MIN")))


def push(topic, title, body, url, urgent):
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "urgent" if urgent else "default",
        "Tags": "rotating_light,ticket" if urgent else "calendar",
        "Click": url,
        "Actions": f"view, Tickets, {url}",
    }
    req = urllib.request.Request(f"{setting('NTFY_SERVER')}/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def popup(title, body):
    import ctypes
    import winsound

    def run():
        for _ in range(3):
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            time.sleep(0.4)
        ctypes.windll.user32.MessageBoxW(0, body, title, 0x40 | 0x40000)  # info + topmost
    threading.Thread(target=run, daemon=True).start()


def alert(topic, ev, urgent):
    when = ev["start"].strftime("%a %d %b %H:%M")
    title = ("🚨 THE LOFT: " if urgent else "The Loft nieuw event: ") + ev["name"]
    body = f"{when}\n{ev['url']}"
    log(("DOEL " if urgent else "NIEUW ") + f"{ev['name']} | {when} | {ev['url']}")
    try:
        push(topic, title, body, ev["url"], urgent)
    except Exception as e:
        log(f"ntfy-push mislukt: {e!r}")
    if urgent and ON_WINDOWS:
        popup(title, body)
        if setting("OPEN_BROWSER") == "1":
            webbrowser.open(ev["url"])


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


# ---------------------------------------------------------------- main

def main():
    cfg = load_json(CONFIG_FILE, {})
    if "ntfy_topic" not in cfg:
        cfg["ntfy_topic"] = "loft-ade-" + secrets.token_hex(6)
        save_json(CONFIG_FILE, cfg)
    topic = os.environ.get("LOFT_NTFY_TOPIC", cfg["ntfy_topic"])

    if "--test" in sys.argv:
        alert(topic, {"name": "TEST Mau P & Dennis Cruz", "start": datetime.now(),
                      "url": "https://theloftamsterdam.com/", "extra": ""}, urgent=True)
        time.sleep(5)
        return

    state = load_json(STATE_FILE, {})
    seen = set(state.get("seen", []))
    baselined = set(state.get("baselined", []))  # bronnen waarvan de bestaande events al vastliggen
    fv = Fourvenues(setting("FV_SLUG"))
    fails = {"fourvenues": 0, "wordpress": 0}
    max_min = int(setting("MAX_RUNTIME_MIN"))
    stop_at = time.time() + max_min * 60 if max_min else None
    shown_topic = topic if ON_WINDOWS else topic[:6] + "…"  # niet volledig in openbare Actions-logs
    log(f"start | ntfy-topic: {shown_topic} | doel: {setting('TARGET_NAMES')} / "
        f"{setting('TARGET_DATE')} {setting('TARGET_TIME')} | elke {setting('POLL_SECONDS')}s")

    while stop_at is None or time.time() < stop_at:
        before = len(seen)
        for source, fetch in (("fourvenues", fv.events), ("wordpress", lambda: wp_events(setting("WP_SITE")))):
            try:
                events = fetch()
            except Exception as e:
                fails[source] += 1
                if fails[source] in (1, 10) or fails[source] % 180 == 0:
                    log(f"{source} mislukt ({fails[source]}x op rij): {e!r}")
                continue
            if fails[source]:
                log(f"{source} weer bereikbaar")
            fails[source] = 0
            for ev in events:
                if ev["key"] in seen:
                    continue
                seen.add(ev["key"])
                if source not in baselined and not is_target(ev):
                    continue  # bestaande events bij de eerste geslaagde ophaalronde alleen onthouden
                alert(topic, ev, urgent=is_target(ev))
            if source not in baselined:
                baselined.add(source)
                log(f"{source}: basislijn vastgelegd ({len(events)} bestaande events)")
            save_json(STATE_FILE, {"seen": sorted(seen), "baselined": sorted(baselined)})
        if len(seen) != before and setting("COMMIT_STATE") == "1":
            commit_state(f"loft-watch: {len(seen) - before} nieuw(e) event(s)")
        time.sleep(int(setting("POLL_SECONDS")))
    log("maximale looptijd bereikt, stop (volgende run neemt het over)")


if __name__ == "__main__":
    main()
