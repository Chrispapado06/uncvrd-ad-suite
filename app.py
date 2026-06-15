"""
UNCVRD Ad Suite — one local app.

Run it:  python3 app.py   →   open http://localhost:4000

Tabs (one window):
  • Dashboard    — the full tracker dashboard (KPIs, charts, leaderboard, Bernard).
  • Data Analyst — Managed Agent: runs Python, makes charts, scoped per creator or all.
  • Settings     — every credential the project needs + your creator roster.

Long analyses run as background jobs the page polls, so the browser never times out.
Credentials are stored locally in credentials.json (git-ignored) — never uploaded.
"""

import base64
import csv
import datetime
import hashlib
import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from anthropic import Anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
IDS_PATH = os.path.join(HERE, "agent.json")
SAMPLE_CSV = os.path.join(HERE, "sample-daily-log.csv")
CONFIG_PATH = os.path.join(HERE, "credentials.json")
CREATORS_PATH = os.path.join(HERE, "creators.json")
# dashboard.html sits one level up locally, or next to app.py in a deploy bundle
DASHBOARD_PATH = next((p for p in [os.path.join(HERE, "dashboard.html"),
                                   os.path.normpath(os.path.join(HERE, "..", "dashboard.html"))]
                       if os.path.exists(p)), os.path.join(HERE, "dashboard.html"))
MA_BETA = "managed-agents-2026-04-01"
PORT = int(os.environ.get("PORT", "4000"))          # cloud hosts inject $PORT
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")    # set on the host → enables login
SESSION_TOKEN = hashlib.sha256(("uncvrd-session:" + APP_PASSWORD).encode()).hexdigest()[:32]

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>UNCVRD Ad Suite — Login</title>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0d12;color:#e8ebf2;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
.box{background:#14171e;border:1px solid #262b36;border-radius:16px;padding:30px 26px;width:min(360px,92vw);box-shadow:0 12px 32px rgba(0,0,0,.5);text-align:center}
.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,#7c5cff,#a78bfa);margin:0 auto 14px;box-shadow:0 2px 10px rgba(124,92,255,.4)}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-.2px}h1 span{background:linear-gradient(135deg,#7c5cff,#a78bfa);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
p{color:#8b94a5;font-size:13px;margin:0 0 18px}
input{width:100%;background:#1b1f28;border:1px solid #262b36;border-radius:10px;color:#e8ebf2;padding:13px 14px;font-size:16px;margin-bottom:12px}
input:focus{outline:none;border-color:#7c5cff;box-shadow:0 0 0 3px rgba(124,92,255,.22)}
button{width:100%;background:#7c5cff;border:0;border-radius:10px;color:#fff;font-weight:700;font-size:15px;padding:13px;cursor:pointer}
button:active{transform:scale(.98)}.err{color:#f06a6a;font-size:13px;margin-bottom:12px}
</style></head><body>
<form class="box" method="post" action="/login">
<div class="logo"></div>
<h1>UNCVRD <span>Ad Suite</span></h1>
<p>Enter the password to continue</p>
<!--ERR-->
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password" inputmode="text">
<button type="submit">Log in</button>
</form></body></html>"""

LIVE_COLS = ["date", "platform", "creator", "campaign", "test",
             "variant", "of_link", "spend", "clicks", "new_fans", "revenue"]
SECRET_KEYS = ["anthropic_key", "supabase_anon_key", "onlyfans_key", "meta_token", "amplitude_token"]
PLAIN_KEYS = ["supabase_url", "meta_ad_acct"]

JOBS = {}  # job_id -> {status, result|error}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            return json.load(open(CONFIG_PATH))
        except Exception:
            return {}
    return {}


def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)


def load_creators():
    if os.path.exists(CREATORS_PATH):
        try:
            return json.load(open(CREATORS_PATH))
        except Exception:
            pass
    return ["Marissa", "Emma", "Maylee"]


def save_creators(lst):
    json.dump(lst, open(CREATORS_PATH, "w"), indent=2)


CONFIG = load_config()
# On a host, secrets come from environment variables (not a file). Env wins.
_ENV_MAP = {"ONLYFANS_API_KEY": "onlyfans_key", "OFAPI_KEY": "onlyfans_key",
            "ANTHROPIC_API_KEY": "anthropic_key", "SUPABASE_URL": "supabase_url",
            "SUPABASE_ANON_KEY": "supabase_anon_key", "META_TOKEN": "meta_token",
            "META_AD_ACCT": "meta_ad_acct", "AMPLITUDE_MCP_TOKEN": "amplitude_token"}
for _ek, _ck in _ENV_MAP.items():
    if os.environ.get(_ek):
        CONFIG[_ck] = os.environ[_ek]
CREATORS = load_creators()


def agent_ids():
    """Managed-agent IDs from agent.json locally, or env vars on a host."""
    if os.path.exists(IDS_PATH):
        try:
            return json.load(open(IDS_PATH))
        except Exception:
            pass
    return {"agent_id": os.environ.get("AGENT_ID"),
            "environment_id": os.environ.get("ENVIRONMENT_ID"),
            "vault_id": os.environ.get("VAULT_ID")}


def api_key():
    return CONFIG.get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY", "")


# ── OnlyFans live auto-sync ──────────────────────────────────────────────────
OF_BASE = "https://app.onlyfansapi.com/api"
LIVE_PATH = os.path.join(HERE, "of_live.json")
LIVE = {"rows": [], "at": None, "error": None}
if os.path.exists(LIVE_PATH):
    try:
        LIVE.update(json.load(open(LIVE_PATH)))
    except Exception:
        pass


def _of_fetch(path):
    key = CONFIG.get("onlyfans_key") or ""
    if not key:
        raise RuntimeError("No OnlyFans API key set (Settings).")
    req = urllib.request.Request(OF_BASE + path, headers={
        "Authorization": "Bearer " + key, "Accept": "application/json",
        "User-Agent": "UNCVRD-AdTracker/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def _of_accounts():
    d = _of_fetch("/accounts")
    if isinstance(d, list):
        return d
    dd = (d or {}).get("data")
    if isinstance(dd, list):
        return dd
    if isinstance(dd, dict):
        return dd.get("list") or []
    return []


def _of_links(aid):
    """Every tracking link for an account. The API paginates (10/page, hasMore flag),
    so we page through with limit=100 until it says there are no more."""
    out = []
    offset = 0
    for _ in range(30):  # safety cap: 30 pages x 100 = 3000 links
        d = _of_fetch("/%s/tracking-links?limit=100&offset=%d" % (aid, offset))
        dd = (d or {}).get("data") or {}
        if isinstance(dd, list):
            out.extend(dd)
            break
        if not isinstance(dd, dict):
            break
        lst = dd.get("list") or []
        out.extend(lst)
        if not dd.get("hasMore") or not lst:
            break
        offset += len(lst)
    return out


def _is_ad_link(name):
    """Hide OnlyFans' auto-generated per-visitor 'Traffic/<id>/#n/<ts>' links from the
    pickers and data — those aren't campaigns you run ads with, they just clutter the list."""
    n = (name or "").strip()
    if not n:
        return True  # unnamed link (shown as c<code>) — keep it
    low = n.lower()
    if low.startswith("traffic/"):
        return False
    if "/#" in n:  # the Traffic/<id>/#<n>/<timestamp> shape
        return False
    return True


def _norm(s):
    """Loose key for matching a Meta campaign name to an OF tracking link."""
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def meta_spend_map():
    """Today's ad spend per campaign from the Meta Marketing API, keyed by a
    normalized campaign name so we can join it to OnlyFans tracking links.
    Returns {} (and never raises) unless META_TOKEN + META_AD_ACCT are set —
    so the whole feature stays dark until your boss adds his token."""
    token = CONFIG.get("meta_token") or ""
    acct = CONFIG.get("meta_ad_acct") or ""
    if not token or not acct:
        return {}
    if not acct.startswith("act_"):
        acct = "act_" + acct
    today = datetime.date.today().isoformat()
    params = urllib.parse.urlencode({
        "level": "campaign",
        "fields": "campaign_name,spend",
        "time_range": json.dumps({"since": today, "until": today}),
        "limit": "500",
        "access_token": token,
    })
    url = "https://graph.facebook.com/v19.0/%s/insights?%s" % (acct, params)
    out = {}
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.load(r)
        for row in (d.get("data") or []):
            k = _norm(row.get("campaign_name"))
            if k:
                out[k] = out.get(k, 0.0) + float(row.get("spend") or 0)
    except Exception as e:
        LIVE["meta_error"] = str(e)
        return {}
    LIVE["meta_error"] = None
    return out


def _classify_platform(name):
    """Which ad platform a tracking link belongs to, from its name."""
    n = (name or "").lower()
    if "meta" in n:
        return "Meta"
    if "guider" in n:
        return "OnlyGuider"
    if "seeker" in n:
        return "OnlySeeker"
    if "finder" in n or "search" in n:
        return "OnlyFinder"
    return None


def meta_spend_daily(since, until):
    """{(date, normalized_campaign): spend} per day from Meta. {} if no token/blocked."""
    token = CONFIG.get("meta_token") or ""
    acct = CONFIG.get("meta_ad_acct") or ""
    if not token or not acct:
        return {}
    if not acct.startswith("act_"):
        acct = "act_" + acct
    params = urllib.parse.urlencode({
        "level": "campaign", "fields": "campaign_name,spend", "time_increment": "1",
        "time_range": json.dumps({"since": since, "until": until}),
        "limit": "1000", "access_token": token})
    try:
        req = urllib.request.Request("https://graph.facebook.com/v19.0/%s/insights?%s" % (acct, params),
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.load(r)
        out = {}
        for row in (d.get("data") or []):
            k = (row.get("date_start"), _norm(row.get("campaign_name")))
            out[k] = out.get(k, 0.0) + float(row.get("spend") or 0)
        return out
    except Exception:
        return {}


FEED_CACHE = {}   # sheet_id -> (timestamp, csv_body)
FEED_COLS = ["Date", "Creator", "Ad Spend Meta", "Ad Spend OnlyFinder", "Ad Spend OnlyGuider",
             "Ad Spend OnlySeeker", "Clicks Meta", "Clicks OnlyFinder", "Clicks OnlyGuider",
             "Clicks OnlySeeker", "Fans Meta", "Fans OnlyFinder", "Fans OnlyGuider",
             "Fans OnlySeeker", "Revenue", "Total Spend", "Total Fans", "ROAS", "Profit"]
PLATS = ["Meta", "OnlyFinder", "OnlyGuider", "OnlySeeker"]


def read_link_overrides(sheet_id):
    """Read the boss's manual choices from the sheet's 'Link Settings' tab.
    Returns {creator_lower|code: platform-or-'ignore'}. {} on any problem (→ auto)."""
    if not sheet_id:
        return {}
    url = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=Link%%20Settings"
           % sheet_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UNCVRD-AdTracker/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        import io
        rows = list(csv.reader(io.StringIO(text)))
        # find the header row (Code in col 3, Override in col 5); bail if this isn't
        # actually the Link Settings tab (gviz returns the first sheet when it's missing)
        hdr = -1
        for i, row in enumerate(rows):
            if len(row) >= 5 and (row[2] or "").strip().lower() == "code" \
               and (row[4] or "").strip().lower().startswith("override"):
                hdr = i
                break
        if hdr < 0:
            return {}
        ov = {}
        for row in rows[hdr + 1:]:
            if len(row) < 5:
                continue
            creator = (row[0] or "").strip().lower()
            code = (row[2] or "").strip()
            choice = (row[4] or "").strip()
            if creator and code and choice:
                ov[creator + "|" + code] = choice
        return ov
    except Exception:
        return {}


def _norm_date(s):
    """Normalize any common date format the sheet might hand us to YYYY-MM-DD."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return s


def read_manual_clicks(sheet_id):
    """Boss's typed daily clicks from the 'Manual Clicks' tab (Date|Creator|Platform|Clicks).
    Returns {(date, creator_lower, platform): clicks}. These REPLACE the auto clicks."""
    if not sheet_id:
        return {}
    url = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=Manual%%20Clicks"
           % sheet_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UNCVRD-AdTracker/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        import io
        rows = list(csv.reader(io.StringIO(text)))
        hdr = -1
        for i, row in enumerate(rows):
            if len(row) >= 4 and (row[0] or "").strip().lower() == "date" \
               and (row[3] or "").strip().lower() == "clicks":
                hdr = i
                break
        if hdr < 0:
            return {}
        mc = {}
        for row in rows[hdr + 1:]:
            if len(row) < 4:
                continue
            date = _norm_date(row[0])
            creator = (row[1] or "").strip().lower()
            plat = (row[2] or "").strip()
            val = (row[3] or "").strip()
            if date and creator and plat in PLATS and val != "":
                try:
                    mc[(date, creator, plat)] = int(float(val))
                except Exception:
                    pass
        return mc
    except Exception:
        return {}


def sheet_feed_csv(days=60, sheet_id=""):
    """Daily per-creator ad numbers as CSV, straight from OnlyFans' per-day stats
    (and Meta when the token works). Stateless: regenerated on request, cached 15 min.
    Honors manual Link Settings overrides from the given sheet."""
    ck = sheet_id or "_"
    cached = FEED_CACHE.get(ck)
    if cached and time.time() - cached[0] < 900:
        return cached[1]
    overrides = read_link_overrides(sheet_id)
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    agg = {}   # (date, creator) -> {"clicks":{p:n}, "fans":{p:n}, "rev":x}
    for a in _of_accounts():
        if not a.get("is_authenticated"):
            continue
        creator = a.get("display_name") or a.get("onlyfans_username") or ""
        try:
            links = _of_links(a["id"])
        except Exception:
            continue
        for l in links:
            # manual override (by creator+code) wins; else auto-detect by name
            ov = overrides.get(creator.lower() + "|" + str(l.get("campaignCode") or ""))
            if ov:
                plat = None if ov.lower() == "ignore" else (ov if ov in PLATS else _classify_platform(l.get("campaignName")))
            else:
                plat = _classify_platform(l.get("campaignName"))
            if not plat or not l.get("id"):
                continue
            try:
                st = _of_fetch("/%s/tracking-links/%s/stats" % (a["id"], l["id"]))
            except Exception:
                continue
            for day in (((st or {}).get("data") or {}).get("daily_metrics") or []):
                ts = day.get("timestamp") or ""
                if ts < start:
                    continue
                k = (ts, creator)
                rec = agg.setdefault(k, {"clicks": {p: 0 for p in PLATS},
                                         "fans": {p: 0 for p in PLATS}, "rev": 0.0})
                rec["clicks"][plat] += int(day.get("clicks") or 0)
                rec["fans"][plat] += int(day.get("subs") or 0)
                rec["rev"] += float(day.get("revenue") or 0)
    # boss's manual daily clicks REPLACE the auto number for that day/creator/platform
    manual_clicks = read_manual_clicks(sheet_id)
    if manual_clicks:
        name_by_lower = {}
        for (ts, cr) in agg:
            name_by_lower.setdefault(cr.lower(), cr)
        for (date, cl, plat), clk in manual_clicks.items():
            cr = name_by_lower.get(cl, cl)
            rec = agg.setdefault((date, cr), {"clicks": {p: 0 for p in PLATS},
                                              "fans": {p: 0 for p in PLATS}, "rev": 0.0})
            rec["clicks"][plat] = clk
    spend = meta_spend_daily(start, datetime.date.today().isoformat())
    lines = [",".join(FEED_COLS)]
    for (ts, creator) in sorted(agg.keys()):
        rec = agg[(ts, creator)]
        ms = 0.0
        nc = _norm(creator)
        for (sd, camp), amt in spend.items():
            if sd == ts and camp and (camp in nc or nc in camp):
                ms += amt
        total_spend = ms
        total_fans = sum(rec["fans"].values())
        roas = (rec["rev"] / total_spend) if total_spend else ""
        row = [ts, creator.replace(",", " "), "%.2f" % ms, "0", "0", "0"]
        row += [str(rec["clicks"][p]) for p in PLATS]
        row += [str(rec["fans"][p]) for p in PLATS]
        row += ["%.2f" % rec["rev"], "%.2f" % total_spend, str(total_fans),
                ("%.2f" % roas) if roas != "" else "", "%.2f" % (rec["rev"] - total_spend)]
        lines.append(",".join(row))
    body = "\n".join(lines) + "\n"
    FEED_CACHE[ck] = (time.time(), body)
    return body


def read_manual_spend(sheet_id):
    """Manual ad spend from the 'Manual Spend' tab (Date|Creator|Platform|Amount).
    Returns {(date, platform): total_$} summed across creators."""
    if not sheet_id:
        return {}
    url = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=Manual%%20Spend"
           % sheet_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UNCVRD-AdTracker/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
        import io
        rows = list(csv.reader(io.StringIO(text)))
        hdr = -1
        for i, row in enumerate(rows):
            if len(row) >= 4 and (row[0] or "").strip().lower() == "date" \
               and (row[2] or "").strip().lower() == "platform":
                hdr = i
                break
        if hdr < 0:
            return {}
        out = {}
        for row in rows[hdr + 1:]:
            if len(row) < 4:
                continue
            date = _norm_date(row[0])
            plat = (row[2] or "").strip()
            amt = (row[3] or "").strip().replace("$", "").replace(",", "")
            if date and plat in PLATS and amt:
                try:
                    out[(date, plat)] = out.get((date, plat), 0.0) + float(amt)
                except Exception:
                    pass
        return out
    except Exception:
        return {}


FLAT_CACHE = {}
FLAT_COLS = ["Date", "Platform", "Clicks", "Fans", "Spend", "Cost Per Fan", "CPC", "CVR",
             "Attributed Revenue", "Total Link LTV", "ROAS", "Agency Profit"]


def sheet_flat_csv(days=60, sheet_id=""):
    """Flat daily breakdown: one row per (date, platform) across all creators, with
    every metric computed. For the boss's horizontal-table view."""
    ck = sheet_id or "_"
    cached = FLAT_CACHE.get(ck)
    if cached and time.time() - cached[0] < 900:
        return cached[1]
    overrides = read_link_overrides(sheet_id)
    manual_clicks = read_manual_clicks(sheet_id)
    manual_spend = read_manual_spend(sheet_id)
    start = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    percp = {}   # (date, creator_lower, platform) -> {clicks, fans, rev}
    for a in _of_accounts():
        if not a.get("is_authenticated"):
            continue
        creator = (a.get("display_name") or a.get("onlyfans_username") or "").lower()
        try:
            links = _of_links(a["id"])
        except Exception:
            continue
        for l in links:
            ov = overrides.get(creator + "|" + str(l.get("campaignCode") or ""))
            if ov:
                plat = None if ov.lower() == "ignore" else (ov if ov in PLATS else _classify_platform(l.get("campaignName")))
            else:
                plat = _classify_platform(l.get("campaignName"))
            if not plat or not l.get("id"):
                continue
            try:
                st = _of_fetch("/%s/tracking-links/%s/stats" % (a["id"], l["id"]))
            except Exception:
                continue
            for day in (((st or {}).get("data") or {}).get("daily_metrics") or []):
                ts = day.get("timestamp") or ""
                if ts < start:
                    continue
                rec = percp.setdefault((ts, creator, plat), {"clicks": 0, "fans": 0, "rev": 0.0})
                rec["clicks"] += int(day.get("clicks") or 0)
                rec["fans"] += int(day.get("subs") or 0)
                rec["rev"] += float(day.get("revenue") or 0)
    for (date, cl, plat), clk in manual_clicks.items():
        rec = percp.setdefault((date, cl, plat), {"clicks": 0, "fans": 0, "rev": 0.0})
        rec["clicks"] = clk
    flat = {}    # (date, platform) -> {clicks, fans, rev, spend}
    for (date, cl, plat), rec in percp.items():
        f = flat.setdefault((date, plat), {"clicks": 0, "fans": 0, "rev": 0.0, "spend": 0.0})
        f["clicks"] += rec["clicks"]; f["fans"] += rec["fans"]; f["rev"] += rec["rev"]
    for (date, camp), amt in meta_spend_daily(start, datetime.date.today().isoformat()).items():
        flat.setdefault((date, "Meta"), {"clicks": 0, "fans": 0, "rev": 0.0, "spend": 0.0})["spend"] += amt
    for (date, plat), amt in manual_spend.items():
        flat.setdefault((date, plat), {"clicks": 0, "fans": 0, "rev": 0.0, "spend": 0.0})["spend"] += amt
    order = {p: i for i, p in enumerate(PLATS)}
    lines = [",".join(FLAT_COLS)]
    for (date, plat) in sorted(flat.keys(), key=lambda x: (x[0], order.get(x[1], 9)), reverse=True):
        f = flat[(date, plat)]
        c, fa, rev, sp = f["clicks"], f["fans"], f["rev"], f["spend"]
        num = lambda v: ("%.2f" % v) if v != "" else ""
        cpf = (sp / fa) if fa else ""
        cpc = (sp / c) if c else ""
        cvr = ("%.1f%%" % (100.0 * fa / c)) if c else ""
        ltv = (rev / fa) if fa else ""
        roas = (rev / sp) if sp else ""
        lines.append(",".join([date, plat, str(c), str(fa), num(sp), num(cpf), num(cpc),
                               cvr, num(rev), num(ltv), num(roas), num(rev - sp)]))
    body = "\n".join(lines) + "\n"
    FLAT_CACHE[ck] = (time.time(), body)
    return body


def sync_onlyfans():
    """Pull every authenticated account's tracking links into live rows the
    dashboard + analyst read. Backend (clicks / new fans / revenue) is live;
    spend is joined from Meta when META_TOKEN is set, else stays 0."""
    try:
        accts = _of_accounts()
    except Exception as e:
        LIVE["error"] = str(e)
        return {"rows": LIVE.get("rows", []), "at": LIVE.get("at"), "error": str(e)}
    today = datetime.date.today().isoformat()
    selected = set(CONFIG.get("selected_accounts") or [])
    spend_map = meta_spend_map()  # {} unless boss's Meta token is set
    rows = []
    for a in accts:
        if not a.get("is_authenticated"):
            continue
        if a["id"] not in selected:
            continue  # only the creators the boss chose in Settings (none until he picks)
        creator = a.get("display_name") or a.get("onlyfans_username") or ""
        try:
            links = _of_links(a["id"])
        except Exception:
            continue
        for l in links:
            if not _is_ad_link(l.get("campaignName")):
                continue  # skip OnlyFans' auto-generated Traffic/... junk links
            name = l.get("campaignName") or ("c" + str(l.get("campaignCode") or ""))
            rev = (l.get("revenue") or {}).get("total") or 0
            rows.append({
                "date": today, "platform": "", "creator": creator,
                "campaign": name, "test": "", "variant": name, "of_link": name,
                "code": str(l.get("campaignCode") or ""),
                # spend stays 0 here — the dashboard attaches Meta spend per creator
                # using meta_spend (campaigns are named by creator), so it lands on
                # the right Meta links without double-counting.
                "spend": 0, "clicks": int(l.get("clicksCount") or 0),
                "new_fans": int(l.get("subscribersCount") or 0), "revenue": float(rev),
            })
    LIVE["rows"] = rows
    LIVE["meta_spend"] = spend_map  # {campaign_name: spend_today}; {} until Meta token set
    LIVE["at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    LIVE["error"] = None
    try:
        json.dump(LIVE, open(LIVE_PATH, "w"))
    except Exception:
        pass
    return {"rows": rows, "at": LIVE["at"], "error": None}


def _sync_loop():
    while True:
        try:
            sync_onlyfans()
        except Exception:
            pass
        time.sleep(6 * 3600)  # refresh every 6 hours


def load_default_data():
    """Default data when nothing is uploaded: live OnlyFans data if synced,
    then Supabase if configured, else the built-in sample."""
    rows = LIVE.get("rows") or []
    if rows:
        path = os.path.join(tempfile.gettempdir(), "live-of-data.csv")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LIVE_COLS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in LIVE_COLS})
        return path, f"live OnlyFans data ({len(rows)} links)"
    url, key = CONFIG.get("supabase_url"), CONFIG.get("supabase_anon_key")
    if url and key:
        try:
            q = url.rstrip("/") + "/rest/v1/ad_daily_log?select=" + ",".join(LIVE_COLS) + "&order=date.asc"
            req = urllib.request.Request(q, headers={"apikey": key, "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=20) as r:
                rows = json.load(r)
            if rows:
                path = os.path.join(tempfile.gettempdir(), "live-ad-data.csv")
                with open(path, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=LIVE_COLS)
                    w.writeheader()
                    for row in rows:
                        w.writerow({c: row.get(c, "") for c in LIVE_COLS})
                return path, f"live data from Supabase ({len(rows)} rows)"
        except Exception as e:
            return SAMPLE_CSV, f"built-in sample (couldn't reach Supabase: {e})"
    return SAMPLE_CSV, "built-in sample data"


ANALYST_BRIEF = (
    "CONTEXT: This is UNCVRD ad-tracking data — one row per OnlyFans tracking link (a 'variant'). "
    "Columns: date, platform (Meta/OnlyFinder), creator, campaign, test, variant (== the tracking-link name), "
    "of_link, spend, clicks, new_fans, revenue.\n"
    "Compute these per variant and per creator: CPC = spend/clicks, CAC = spend/new_fans, "
    "click-to-sub rate = new_fans/clicks, LTV = revenue/new_fans, ROAS = revenue/spend, profit = revenue - spend.\n"
    "DECISION RULE: SCALE a variant when ROAS >= 2.0, CUT when ROAS < 1.0, otherwise KEEP.\n"
    "RIGOR: Weigh sample size — treat calls based on fewer than ~100 clicks or ~10 new fans as low-confidence and say so. "
    "Don't claim significance you can't support.\n"
    "IMPORTANT: If spend is 0 or missing for rows (Meta isn't connected yet), you CANNOT compute ROAS/CAC/profit for "
    "those — state that plainly and rank instead by revenue, new fans, click-to-sub rate, and LTV. Do not invent spend.\n"
    "Use the real creator and variant names and the actual numbers. Save any charts to /mnt/session/outputs/. "
    "Finish with a short, prioritized, specific ACTION LIST (what to scale, cut, and test next).\n\n"
    "QUESTION: "
)


def scope_prefix(scope):
    if not scope or scope == "all":
        return ""
    if scope == "each":
        return "Break your analysis down by each creator and compare them. "
    return f"Focus your analysis only on the creator named '{scope}'. "


KEYWORD_SHOT_TOOL = {
    "name": "report_keywords",
    "description": "Report every keyword / search-term row visible in an OnlyFinder, "
                   "OnlyGuider or OnlySeeker advertising screenshot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "keyword": {"type": "string", "description": "the keyword / search term text exactly as shown"},
                        "status": {"type": "string", "enum": ["working", "not_working", "unknown"],
                                   "description": "working = active/enabled/approved/getting clicks; not_working = paused/disabled/rejected/zero results; unknown if unclear"},
                        "clicks": {"type": ["integer", "null"], "description": "clicks if a number is shown, else null"},
                        "spend": {"type": ["number", "null"], "description": "USD spent if shown, else null"},
                    },
                    "required": ["keyword", "status"],
                },
            }
        },
        "required": ["keywords"],
    },
}


def extract_keywords_from_image(b64, media_type, platform):
    """Use Claude vision to read keyword rows out of a directory-ad screenshot.
    Returns a list of {keyword, status, clicks, spend}. Raises on API error."""
    client = Anthropic(api_key=api_key())
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        tools=[KEYWORD_SHOT_TOOL],
        tool_choice={"type": "tool", "name": "report_keywords"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": (
                "This is a screenshot from the %s advertising / keyword dashboard for an "
                "OnlyFans creator. Read EVERY keyword (search-term) row you can see. For each, "
                "capture the keyword text, whether it looks like it is working (active / enabled / "
                "approved / getting clicks) or not working (paused / disabled / rejected / zero "
                "results), and any clicks and spend ($) numbers shown. If a number isn't visible, "
                "use null. Call the report_keywords tool with the full list." % platform)},
        ]}],
    )
    for b in msg.content:
        if getattr(b, "type", None) == "tool_use" and b.name == "report_keywords":
            return (b.input or {}).get("keywords", []) or []
    return []


def run_analysis(key, question, data_path):
    client = Anthropic(api_key=key)
    ids = agent_ids()
    uploaded = client.beta.files.upload(file=open(data_path, "rb"))
    base = os.path.basename(data_path)
    mount = f"/workspace/{base}"
    session = client.beta.sessions.create(
        agent=ids["agent_id"],
        environment_id=ids["environment_id"],
        resources=[{"type": "file", "file_id": uploaded.id, "mount_path": mount}],
        vault_ids=[ids["vault_id"]] if ids.get("vault_id") else [],
        title="Data analysis",
    )
    q = f"The dataset is mounted at {mount}. {question}"
    stream = client.beta.sessions.events.stream(session_id=session.id)
    client.beta.sessions.events.send(
        session_id=session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": q}]}],
    )
    parts = []
    for event in stream:
        t = getattr(event, "type", None)
        if t == "agent.message":
            for b in getattr(event, "content", None) or []:
                if getattr(b, "type", None) == "text":
                    parts.append(b.text)
        elif t == "session.error":
            parts.append("\n[error] " + getattr(getattr(event, "error", None), "message", "unknown"))
        elif t == "session.status_terminated":
            break
        elif t == "session.status_idle":
            sr = getattr(event, "stop_reason", None)
            if sr is not None and getattr(sr, "type", None) == "requires_action":
                continue
            break
    images = []
    try:
        for f in client.beta.files.list(scope_id=session.id, betas=[MA_BETA]).data:
            name = f.filename or ""
            ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
            if ext in ("png", "jpg", "jpeg", "gif", "svg"):
                data = client.beta.files.download(f.id).read()
                mime = "image/svg+xml" if ext == "svg" else f"image/{'jpeg' if ext == 'jpg' else ext}"
                images.append({"name": name, "data_url": f"data:{mime};base64," + base64.b64encode(data).decode()})
    except Exception:
        pass
    try:
        client.beta.files.delete(uploaded.id)
    except Exception:
        pass
    return {"answer": "\n".join(parts).strip() or "(no text returned)", "images": images, "session": session.id}


def start_job(question, data_path, source):
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "running"}

    def work():
        try:
            res = run_analysis(api_key(), question, data_path)
            res["source"] = source
            JOBS[job_id] = {"status": "done", "result": res}
        except Exception as e:
            JOBS[job_id] = {"status": "error", "error": str(e)}

    threading.Thread(target=work, daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        if "?" in self.path:
            return urllib.parse.parse_qs(self.path.split("?", 1)[1])
        return {}

    def _authed(self):
        if not APP_PASSWORD:
            return True  # no password set (local) → open
        return ("uncvrd_auth=" + SESSION_TOKEN) in self.headers.get("Cookie", "")

    def _login(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        pw = urllib.parse.parse_qs(raw.decode("utf-8", "ignore")).get("password", [""])[0]
        if APP_PASSWORD and pw == APP_PASSWORD:
            self.send_response(302)
            self.send_header("Set-Cookie", "uncvrd_auth=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000" % SESSION_TOKEN)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(401, LOGIN_HTML.replace("<!--ERR-->", '<div class="err">Wrong password — try again.</div>'),
                       "text/html; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # Today's total Meta spend — for the Google Sheet (Meta blocks Google's IPs,
        # so the sheet asks us instead). Key-protected, no login cookie needed.
        if path == "/meta-today":
            key = (self._query().get("key") or [""])[0]
            if not APP_PASSWORD or key != APP_PASSWORD:
                return self._send(403, {"error": "forbidden"})
            try:
                m = meta_spend_map() or {}
                total = round(sum(float(v) for v in m.values()), 2)
                return self._send(200, {"spend": total, "by_campaign": m})
            except Exception as e:
                return self._send(200, {"spend": 0, "by_campaign": {}, "error": str(e)})
        # Daily per-creator CSV for the Google Sheet (pulled via IMPORTDATA).
        if path == "/sheet-feed":
            key = (self._query().get("key") or [""])[0]
            if not APP_PASSWORD or key != APP_PASSWORD:
                return self._send(403, {"error": "forbidden"})
            sid = (self._query().get("sheet") or [""])[0]
            try:
                return self._send(200, sheet_feed_csv(sheet_id=sid), "text/csv; charset=utf-8")
            except Exception as e:
                return self._send(200, "Date,Creator\nerror,%s\n" % str(e).replace(",", " "), "text/csv; charset=utf-8")
        # Flat daily breakdown (one row per date+platform) for the boss's horizontal table.
        if path == "/sheet-flat":
            key = (self._query().get("key") or [""])[0]
            if not APP_PASSWORD or key != APP_PASSWORD:
                return self._send(403, {"error": "forbidden"})
            sid = (self._query().get("sheet") or [""])[0]
            try:
                return self._send(200, sheet_flat_csv(sheet_id=sid), "text/csv; charset=utf-8")
            except Exception as e:
                return self._send(200, "Date,Creator\nerror,%s\n" % str(e).replace(",", " "), "text/csv; charset=utf-8")
        if not self._authed():
            return self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
        if path == "/":
            return self._send(200, SHELL_HTML, "text/html; charset=utf-8")
        if path == "/dashboard":
            try:
                with open(DASHBOARD_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(200, f"<body style='background:#0b0d10;color:#e7eaf0;font:16px sans-serif;padding:40px'>Couldn't load dashboard.html ({e}).</body>", "text/html; charset=utf-8")
        if path == "/config":
            return self._send(200, {
                "creators": CREATORS,
                "supabase_url": CONFIG.get("supabase_url", ""),
                "meta_ad_acct": CONFIG.get("meta_ad_acct", ""),
                "has": {k: bool(CONFIG.get(k)) for k in SECRET_KEYS},
                "agent_ready": bool(agent_ids().get("agent_id")),
            })
        if path == "/result":
            jid = (self._query().get("id") or [""])[0]
            return self._send(200, JOBS.get(jid, {"status": "unknown"}))
        if path == "/livedata":
            return self._send(200, {"rows": LIVE.get("rows", []), "at": LIVE.get("at"),
                                    "error": LIVE.get("error"), "meta_spend": LIVE.get("meta_spend") or {}})
        if path == "/of-accounts":
            try:
                sel = set(CONFIG.get("selected_accounts") or [])
                out = [{"id": a["id"], "name": a.get("display_name") or a.get("onlyfans_username") or "",
                        "username": a.get("onlyfans_username") or "", "authed": bool(a.get("is_authenticated")),
                        "selected": a["id"] in sel} for a in _of_accounts()]
                return self._send(200, {"accounts": out})
            except Exception as e:
                return self._send(200, {"accounts": [], "error": str(e)})
        if path == "/of-links":
            aid = (self._query().get("account") or [""])[0]
            if not aid:
                return self._send(200, {"links": []})
            try:
                links = [{"code": str(l.get("campaignCode") or ""),
                          "name": l.get("campaignName") or ("c" + str(l.get("campaignCode") or "")),
                          "clicks": int(l.get("clicksCount") or 0),
                          "subs": int(l.get("subscribersCount") or 0)}
                         for l in _of_links(aid) if _is_ad_link(l.get("campaignName"))]
                links.sort(key=lambda x: (-x["subs"], -x["clicks"]))  # most-active first
                return self._send(200, {"links": links})
            except Exception as e:
                return self._send(200, {"links": [], "error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/login":
            return self._login()
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        path = self.path.split("?", 1)[0]

        if path == "/config":
            for k in SECRET_KEYS:
                v = (payload.get(k) or "").strip()
                if v:  # only overwrite a secret when a new value is given
                    CONFIG[k] = v
            for k in PLAIN_KEYS:
                if k in payload:
                    CONFIG[k] = (payload.get(k) or "").strip()
            resync = False
            if "selected_accounts" in payload:
                CONFIG["selected_accounts"] = payload.get("selected_accounts") or []
                resync = True
                try:  # keep the analyst's creator scope list in sync with the picks
                    idmap = {a["id"]: (a.get("display_name") or a.get("onlyfans_username") or "") for a in _of_accounts()}
                    CREATORS[:] = [idmap[i] for i in CONFIG["selected_accounts"] if idmap.get(i)]
                    save_creators(CREATORS)
                except Exception:
                    pass
            save_config(CONFIG)
            if resync:
                try:
                    sync_onlyfans()  # refresh live data for the new creator selection
                except Exception:
                    pass
            return self._send(200, {"ok": True})

        if path == "/keyword-shot":
            if not api_key():
                return self._send(400, {"error": "No Anthropic API key configured on the server."})
            img = payload.get("image") or ""
            platform = (payload.get("platform") or "OnlyFinder").strip()
            try:
                if img.startswith("data:"):
                    head, b64 = img.split(",", 1)
                    media_type = head.split(";")[0].split(":", 1)[1] or "image/png"
                else:
                    b64, media_type = img, "image/png"
                if not b64:
                    return self._send(400, {"error": "No image received."})
                kws = extract_keywords_from_image(b64, media_type, platform)
                return self._send(200, {"keywords": kws})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if path == "/sync":
            return self._send(200, sync_onlyfans())

        if path == "/creators":
            CREATORS[:] = [c for c in (payload.get("creators") or []) if str(c).strip()]
            save_creators(CREATORS)
            return self._send(200, {"creators": CREATORS})

        if path == "/analyze":
            if not api_key():
                return self._send(400, {"error": "No Anthropic API key — add it in Settings."})
            if not agent_ids().get("agent_id"):
                return self._send(400, {"error": "No agent configured (run setup_agent.py, or set AGENT_ID/ENVIRONMENT_ID)."})
            question = (payload.get("question") or "").strip()
            if not question:
                return self._send(400, {"error": "Type a question first."})
            question = scope_prefix(payload.get("scope")) + ANALYST_BRIEF + question
            csv_text = payload.get("csv_text") or ""
            filename = os.path.basename(payload.get("filename") or "data.csv")
            try:
                if csv_text.strip():
                    data_path = os.path.join(tempfile.gettempdir(), filename)
                    with open(data_path, "w") as f:
                        f.write(csv_text)
                    source = f"uploaded file ({filename})"
                else:
                    data_path, source = load_default_data()
                return self._send(200, {"job_id": start_job(question, data_path, source)})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


SHELL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UNCVRD Ad Suite</title>
<style>
:root{--bg:#0b0d12;--panel:#14171e;--panel2:#1b1f28;--line:#262b36;--text:#e8ebf2;--muted:#8b94a5;--accent:#7c5cff;--accent2:#a78bfa;--green:#37d399;--red:#f06a6a;--shadow:0 1px 2px rgba(0,0,0,.35);--shadow-lg:0 10px 30px rgba(0,0,0,.45);--kpi-accent:linear-gradient(135deg,#7c5cff,#a78bfa);--radius:16px}
[data-theme="light"]{--bg:#f5f7fa;--panel:#ffffff;--panel2:#eef1f6;--line:#e4e8ef;--text:#161922;--muted:#69727f;--accent:#6d4bff;--accent2:#8b6cff;--green:#0a9d5e;--red:#d6455a;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.07);--shadow-lg:0 12px 32px rgba(16,24,40,.12);--kpi-accent:linear-gradient(135deg,#6d4bff,#8b6cff)}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
header{display:flex;align-items:center;gap:22px;padding:0 22px;height:56px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
.brand{font-size:16px;font-weight:700}.brand span{color:var(--accent)}
nav{display:flex;gap:6px}
.tab{background:transparent;border:1px solid transparent;color:var(--muted);border-radius:9px;padding:8px 14px;font-size:13.5px;font-weight:600;cursor:pointer}
.tab:hover{color:var(--text)}.tab.active{color:var(--text);background:var(--panel2);border-color:var(--line)}
.view{height:calc(100vh - 56px);overflow:auto}
#dash{width:100%;height:100%;border:0;display:block}
main{max-width:880px;margin:0 auto;padding:22px 24px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:16px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:7px}
input,textarea,select,button.primary{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px 12px;font-size:14px;font-family:inherit}
input,textarea,select{width:100%}textarea{min-height:74px;resize:vertical}
button{cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;padding:11px 18px}
button.primary:disabled{opacity:.5;cursor:default}
.ghost{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 13px;font-weight:600}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.field{margin-bottom:14px}
.muted{color:var(--muted)}.src-note{font-size:12.5px;color:var(--muted)}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin:10px 0}
.chip{background:var(--panel2);border:1px solid var(--line);color:var(--muted);border-radius:999px;padding:6px 12px;font-size:12.5px;cursor:pointer}
.chip:hover{color:var(--text);border-color:var(--accent)}
#answer{white-space:pre-wrap;font-size:13.6px;line-height:1.6}#answer b{color:var(--text)}
.spinner{width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;display:inline-block;animation:spin 1s linear infinite;vertical-align:-3px;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
.imgwrap{margin-top:14px}.imgwrap img{max-width:100%;border:1px solid var(--line);border-radius:10px;background:#fff}
.err{color:var(--red)}.ok{color:var(--green)}
details{margin-top:12px}details summary{cursor:pointer;color:var(--accent);font-size:13px}
.crow{display:flex;align-items:center;gap:8px;background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:7px 12px;margin-bottom:7px}
.crow .x{margin-left:auto;color:var(--muted);cursor:pointer;font-size:16px;line-height:1}
.crow .x:hover{color:var(--red)}
a{color:var(--accent)}
/* ── modern + theming polish ── */
body{transition:background .25s ease,color .25s ease;-webkit-font-smoothing:antialiased}
header{backdrop-filter:saturate(140%) blur(8px);transition:background .25s,border-color .25s}
.brand{display:flex;align-items:center;gap:9px;letter-spacing:-.2px}
.brand .logo{width:24px;height:24px;border-radius:7px;background:var(--kpi-accent);display:inline-block;box-shadow:0 2px 8px rgba(124,92,255,.35)}
.brand .grad{background:var(--kpi-accent);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.icon-btn{margin-left:auto;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:10px;width:38px;height:34px;display:inline-flex;align-items:center;justify-content:center;font-size:15px;cursor:pointer;transition:.15s}
.icon-btn:hover{border-color:var(--accent)}
.tab{transition:.15s}
.card{border-radius:var(--radius);box-shadow:var(--shadow);transition:background .25s,border-color .2s,box-shadow .2s}
.card:hover{box-shadow:var(--shadow-lg)}
input,textarea,select{transition:border-color .15s,box-shadow .15s,background .25s,color .25s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,92,255,.22)}
button.primary{transition:.15s}button.primary:hover:not(:disabled){filter:brightness(1.08)}
.chip{transition:.15s}.ghost{transition:.15s}.ghost:hover{border-color:var(--accent)}
.crow{transition:.15s}.crow:hover{border-color:var(--accent)}
/* modern custom dropdowns (no OS chrome) */
select{-webkit-appearance:none;-moz-appearance:none;appearance:none;cursor:pointer;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b94a5' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 11px center;background-size:11px;padding-right:32px}
select:hover{border-color:var(--accent)}
/* modern button hover (dark → white + bigger) with moving rainbow halo */
button.primary,.ghost,.icon-btn,.chip{position:relative;z-index:0;transition:transform .16s cubic-bezier(.2,.7,.3,1),background-color .16s ease,color .16s ease,box-shadow .16s ease,border-color .16s ease}
button.primary:hover,.ghost:hover,.icon-btn:hover,.chip:hover{background:#fff;color:#111;border-color:#fff;transform:scale(1.06);box-shadow:0 8px 22px rgba(0,0,0,.30);filter:none}
button.primary:active,.ghost:active,.icon-btn:active,.chip:active{transform:scale(.96)}
@property --bang{syntax:"<angle>";initial-value:0deg;inherits:false}
button.primary::before,.ghost::before,.icon-btn::before,.chip::before{content:"";position:absolute;inset:-1.5px;border-radius:inherit;z-index:-1;opacity:0;padding:1.25px;background:conic-gradient(from var(--bang),#ff004c,#ff8a00,#ffe600,#28ff00,#00e1ff,#5b6bff,#d400ff,#ff004c);-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);mask-composite:exclude;transition:opacity .2s ease;animation:huebang 2.6s linear infinite}
button.primary:hover::before,.ghost:hover::before,.icon-btn:hover::before,.chip:hover::before{opacity:1}
@keyframes huebang{to{--bang:360deg}}
/* mobile */
body{overflow-x:hidden}
@media(max-width:600px){
  header{height:auto;min-height:52px;flex-wrap:wrap;padding:10px 14px;gap:8px}
  .brand{font-size:15px}
  nav{order:3;width:100%;overflow-x:auto;gap:4px}
  .icon-btn{margin-left:auto}
  .view{height:auto;min-height:calc(100vh - 112px);overflow:visible}
  #dash{height:calc(100vh - 112px)}
  main{padding:16px 14px}
  .row .field{min-width:0}
}
</style></head>
<body>
<header>
  <div class="brand"><i class="logo"></i>UNCVRD <span class="grad">Ad Suite</span></div>
  <nav>
    <button class="tab active" data-view="overview">Overview</button>
    <button class="tab" data-view="creators">Creators</button>
    <button class="tab" data-view="analyst">Data Analyst</button>
    <button class="tab" data-view="settings">Settings</button>
  </nav>
  <button id="themeBtn" class="icon-btn" title="Toggle light / dark mode">🌙</button>
</header>

<div id="view-dashboard" class="view"><iframe id="dash" src="/dashboard"></iframe></div>

<div id="view-analyst" class="view" hidden><main>
  <div class="card" id="needKey" style="display:none">
    <label>Heads up</label>
    <div class="src-note">Add your Anthropic API key in <a href="#" id="toSettings">Settings</a> to use the analyst.</div>
  </div>
  <div class="card" id="analystCard">
    <div class="field">
      <label>Analyze</label>
      <select id="scope"></select>
      <div class="src-note" style="margin-top:6px">Pick one creator, compare each, or analyze everyone together.</div>
    </div>
    <div class="field">
      <label>Question</label>
      <textarea id="q" placeholder="e.g. Which variant has the best ROAS, and is the gap meaningful given the sample size? Plot revenue vs spend."></textarea>
      <div class="chips">
        <span class="chip" data-q="Which variant has the best ROAS, and is the gap meaningful given the sample size?">Best ROAS</span>
        <span class="chip" data-q="Plot daily revenue vs spend and tell me the trend.">Revenue vs spend chart</span>
        <span class="chip" data-q="Rank creators by profit and show a bar chart.">Creator profit chart</span>
        <span class="chip" data-q="Which variants should I cut, and how much am I losing on them?">What to cut</span>
      </div>
    </div>
    <button class="primary" id="run">Analyze</button>
    <div class="src-note" style="margin-top:10px">Uses your tracker data automatically (live from Supabase if connected, else the built-in sample).</div>
    <details>
      <summary>Advanced — analyze a different file instead</summary>
      <input id="file" type="file" accept=".csv,.json,.tsv,.txt" style="margin-top:10px">
    </details>
  </div>
  <div class="card" id="resultCard" style="display:none">
    <label>Result</label>
    <div id="status"></div>
    <div id="answer"></div>
    <div id="images" class="imgwrap"></div>
    <div class="src-note" id="sessionLine" style="margin-top:12px"></div>
  </div>
</main></div>

<div id="view-settings" class="view" hidden><main>
  <div class="card">
    <label>Credentials</label>
    <div class="src-note" style="margin-bottom:14px">Keys are already loaded securely on the server — a field showing <b>•••• saved</b> is set and working; <b>leave it blank</b>. Only type in a field if you want to <b>replace</b> that key (e.g. add the Meta token).</div>
    <div class="field"><label>Anthropic API key <span class="muted" style="text-transform:none">· powers the analyst &amp; Bernard</span></label><input id="c_anthropic" type="password" placeholder="sk-ant-..."></div>
    <div class="row"><div class="field" style="flex:1;min-width:240px"><label>Supabase URL</label><input id="c_supabase_url" type="text" placeholder="https://xxxx.supabase.co"></div>
    <div class="field" style="flex:1;min-width:240px"><label>Supabase anon key</label><input id="c_supabase_anon_key" type="password" placeholder="anon key — turns on live data"></div></div>
    <div class="field"><label>OnlyFans API key</label><input id="c_onlyfans_key" type="password" placeholder="OFAPI key"></div>
    <div class="row"><div class="field" style="flex:1;min-width:240px"><label>Meta access token</label><input id="c_meta_token" type="password" placeholder="Meta/Facebook token"></div>
    <div class="field" style="flex:1;min-width:240px"><label>Meta ad account id</label><input id="c_meta_ad_acct" type="text" placeholder="without act_ prefix"></div></div>
    <div class="field"><label>Amplitude MCP token</label><input id="c_amplitude_key" type="password" placeholder="Amplitude MCP OAuth token"></div>
    <button class="primary" id="saveCfg">Save credentials</button>
    <span class="src-note ok" id="cfgSaved" style="margin-left:12px"></span>
  </div>
</main></div>

<script>
const $ = id => document.getElementById(id);
let creators = [], hasKey = false;

document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>setView(t.dataset.view)));
function tellDashView(v){ const f=$("dash"); if(f&&f.contentWindow){ try{ f.contentWindow.postMessage({type:"dview",view:v},"*"); }catch(e){} } }
function setView(v){
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x.dataset.view===v));
  const dash=(v==="overview"||v==="creators");
  $("view-dashboard").hidden=!dash;
  $("view-analyst").hidden=(v!=="analyst");
  $("view-settings").hidden=(v!=="settings");
  if(dash) tellDashView(v);
  if(v==="analyst") refreshScope();
}
async function refreshScope(){ try{ const d=await(await fetch("/config")).json(); creators=d.creators||[]; renderScope(); }catch(e){} }
$("toSettings").addEventListener("click",e=>{e.preventDefault();setView("settings");});

async function loadConfig(){
  const d = await (await fetch("/config")).json();
  creators = d.creators||[]; hasKey = !!(d.has&&d.has.anthropic_key);
  $("c_supabase_url").value = d.supabase_url||"";
  $("c_meta_ad_acct").value = d.meta_ad_acct||"";
  const ph = (id,has)=>{ if(has) $(id).placeholder = "•••••••• saved"; };
  ph("c_anthropic",d.has.anthropic_key); ph("c_supabase_anon_key",d.has.supabase_anon_key);
  ph("c_onlyfans_key",d.has.onlyfans_key); ph("c_meta_token",d.has.meta_token); ph("c_amplitude_key",d.has.amplitude_token);
  $("needKey").style.display = hasKey ? "none":"block";
  $("analystCard").style.opacity = hasKey ? "1":".5";
  renderScope();
}
function renderScope(){
  const s=$("scope"); s.innerHTML="";
  const opts=[["all","All creators (together)"],["each","Each creator (compare)"]].concat(creators.map(c=>[c,c]));
  opts.forEach(([v,l])=>{const o=document.createElement("option");o.value=v;o.textContent=l;s.appendChild(o);});
}

$("saveCfg").addEventListener("click",async()=>{
  const body={
    anthropic_key:$("c_anthropic").value, supabase_url:$("c_supabase_url").value,
    supabase_anon_key:$("c_supabase_anon_key").value, onlyfans_key:$("c_onlyfans_key").value,
    meta_token:$("c_meta_token").value, meta_ad_acct:$("c_meta_ad_acct").value, amplitude_key:$("c_amplitude_key").value
  };
  await fetch("/config",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)});
  $("cfgSaved").textContent="Saved ✓"; setTimeout(()=>$("cfgSaved").textContent="",2500);
  ["c_anthropic","c_supabase_anon_key","c_onlyfans_key","c_meta_token","c_amplitude_key"].forEach(id=>$(id).value="");
  loadConfig();
});

// analyst
let fileText="", fileName="";
$("file").addEventListener("change",e=>{const f=e.target.files[0]; if(!f){fileText="";fileName="";return;}
  fileName=f.name; const rd=new FileReader(); rd.onload=ev=>fileText=ev.target.result; rd.readAsText(f);});
document.querySelectorAll("#view-analyst .chip").forEach(c=>c.addEventListener("click",()=>{$("q").value=c.dataset.q;}));
function escapeHtml(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function mdLite(s){return escapeHtml(s).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>");}

$("run").addEventListener("click",async()=>{
  const q=$("q").value.trim(); if(!q){ alert("Type a question first."); return; }
  $("resultCard").style.display="block"; $("answer").innerHTML=""; $("images").innerHTML=""; $("sessionLine").textContent="";
  $("status").innerHTML='<span class="spinner"></span>Working… booting the sandbox, loading data, analyzing (≈30–60s).';
  $("run").disabled=true;
  try{
    const start=await fetch("/analyze",{method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({question:q, scope:$("scope").value, filename:fileName, csv_text:fileText})});
    const sd=await start.json();
    if(!start.ok||sd.error){ $("status").innerHTML='<span class="err">Error: '+escapeHtml(sd.error||("HTTP "+start.status))+'</span>'; $("run").disabled=false; return; }
    poll(sd.job_id);
  }catch(err){ $("status").innerHTML='<span class="err">Network error: '+escapeHtml(err.message)+'</span>'; $("run").disabled=false; }
});

async function poll(jobId){
  try{
    const d=await (await fetch("/result?id="+encodeURIComponent(jobId))).json();
    if(d.status==="running"){ setTimeout(()=>poll(jobId),2500); return; }
    $("run").disabled=false;
    if(d.status==="error"){ $("status").innerHTML='<span class="err">Error: '+escapeHtml(d.error||"failed")+'</span>'; return; }
    if(d.status==="done"){
      const r=d.result||{};
      $("status").innerHTML = r.source ? '<span class="src-note">Analyzed: '+escapeHtml(r.source)+'</span>' : "";
      $("answer").innerHTML=mdLite(r.answer||"(no answer)");
      (r.images||[]).forEach(im=>{const i=document.createElement("img");i.src=im.data_url;i.alt=im.name;$("images").appendChild(i);});
      if(r.session) $("sessionLine").innerHTML='Session '+escapeHtml(r.session)+' · <a target="_blank" href="https://platform.claude.com/workspaces/default/sessions/'+encodeURIComponent(r.session)+'">watch in Console</a>';
      return;
    }
    setTimeout(()=>poll(jobId),2500);
  }catch(err){ setTimeout(()=>poll(jobId),3000); }
}

// theme (themes the whole suite, including the dashboard iframe)
function tellDash(t){ const f=$("dash"); if(f&&f.contentWindow){ try{ f.contentWindow.postMessage({type:"theme",theme:t},"*"); }catch(e){} } }
function applyTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  const b=$("themeBtn"); if(b) b.textContent = t==="light"?"☀️":"🌙";
  localStorage.setItem("suite_theme",t); tellDash(t);
}
$("themeBtn").addEventListener("click",()=>{
  const cur=document.documentElement.getAttribute("data-theme")==="light"?"light":"dark";
  applyTheme(cur==="light"?"dark":"light");
});
$("dash").addEventListener("load",()=>{ tellDash(localStorage.getItem("suite_theme")||"dark"); const a=document.querySelector(".tab.active"); tellDashView(a&&a.dataset.view==="creators"?"creators":"overview"); });
applyTheme(localStorage.getItem("suite_theme")||"dark");

loadConfig();
</script>
</body></html>"""


def _ensure_sample():
    if not os.path.exists(SAMPLE_CSV):
        with open(SAMPLE_CSV, "w") as f:
            f.write(
                "Date,Platform,Creator,Campaign,Test,Variant,OF Link,Spend,Clicks,New Fans,Revenue\n"
                "2026-06-05,OnlyFinder,Marissa,jun-launch,profile pic A,marissa-pic-a,marissa-pic-a,40,120,9,180\n"
                "2026-06-05,OnlyFinder,Marissa,jun-launch,profile pic B,marissa-pic-b,marissa-pic-b,40,95,4,60\n"
                "2026-06-05,Meta,Emma,jun-launch,hook: shy,emma-hook-shy,emma-hook-shy,60,80,6,140\n"
                "2026-06-05,Meta,Emma,jun-launch,hook: bold,emma-hook-bold,emma-hook-bold,60,150,5,70\n"
                "2026-06-05,OnlyFinder,Maylee,jun-launch,keyword: gym,maylee-gym,maylee-gym,25,90,11,320\n"
                "2026-06-05,OnlyFinder,Maylee,jun-launch,keyword: beach,maylee-beach,maylee-beach,25,70,3,40\n"
                "2026-06-06,OnlyFinder,Marissa,jun-launch,profile pic A,marissa-pic-a,marissa-pic-a,45,130,10,210\n"
                "2026-06-06,OnlyFinder,Marissa,jun-launch,profile pic B,marissa-pic-b,marissa-pic-b,30,70,2,25\n"
                "2026-06-06,Meta,Emma,jun-launch,hook: shy,emma-hook-shy,emma-hook-shy,65,85,7,175\n"
                "2026-06-06,Meta,Emma,jun-launch,hook: bold,emma-hook-bold,emma-hook-bold,50,120,4,55\n"
                "2026-06-06,OnlyFinder,Maylee,jun-launch,keyword: gym,maylee-gym,maylee-gym,30,100,13,360\n"
                "2026-06-06,OnlyFinder,Maylee,jun-launch,keyword: beach,maylee-beach,maylee-beach,20,60,2,30\n"
            )


def main():
    _ensure_sample()
    if CONFIG.get("onlyfans_key"):
        threading.Thread(target=_sync_loop, daemon=True).start()  # live OnlyFans pull
    hosted = bool(os.environ.get("PORT"))            # a cloud host injects PORT
    host = "0.0.0.0" if hosted else "127.0.0.1"
    print("\n  UNCVRD Ad Suite  ready")
    print(f"  →  {'serving on 0.0.0.0:%d (hosted)' % PORT if hosted else 'http://localhost:%d' % PORT}")
    print(f"  →  login: {'ON (password set)' if APP_PASSWORD else 'off (no APP_PASSWORD)'}\n")
    if not hosted:
        try:
            webbrowser.open(f"http://localhost:{PORT}")
        except Exception:
            pass
    try:
        ThreadingHTTPServer((host, PORT), Handler).serve_forever()
    except OSError as e:
        print(f"\n  Couldn't start on port {PORT}: {e}\n")
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
