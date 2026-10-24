"""Laddar upp aktiviteterna från st3_to_fit.py till Strava via API:t.

Förberedelse (en gång):
  1. Skapa en API-applikation på https://www.strava.com/settings/api
     - "Authorization Callback Domain": localhost
  2. Kör:  python upload_strava.py --client-id 12345 --client-secret abc...
     Webbläsaren öppnas för inloggning/godkännande; token sparas i strava_token.json.

Skriptet går att avbryta och köra om – redan uppladdade aktiviteter hoppas över
(status sparas i upload_state.json). Vid Stravas rate limit väntar det automatiskt.

Innan uppladdning hämtas dina befintliga Strava-aktiviteter, och aktiviteter som
redan finns där (samma starttid ±10 min eller minst halva tiden överlappande) hoppas över
med status "exists". Aktiviteter som bara har datum (kl. 00:00) matchas mot samma dag
och samma sorts sport.

Flaggor:
  --dry-run       visa vad som skulle laddas upp och vad som redan finns, utan att ändra något
  --no-check      hoppa över kollen mot befintliga Strava-aktiviteter
  --limit N       ladda bara upp N aktiviteter den här körningen (bra för ett första test)
  --no-manual     hoppa över aktiviteter utan spårdata (skapas annars som manuella aktiviteter)
"""
import argparse
import bisect
import datetime as dt
import http.server
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "strava_token.json"
STATE_FILE = HERE / "upload_state.json"
EXISTING_FILE = HERE / "strava_existing.json"
API = "https://www.strava.com/api/v3"
REDIRECT_PORT = 8765

# Sporttyper som Strava inte säkert härleder från FIT-filen; sätts explicit efter uppladdning.
FIX_SPORT = {"TrailRun", "MountainBikeRide", "Kayaking", "Canoeing", "Rowing", "Snowshoe", "Golf", "Hike"}


SPORT_FAMILY = {
    "Run": "run", "TrailRun": "run", "VirtualRun": "run",
    "Ride": "ride", "MountainBikeRide": "ride", "GravelRide": "ride", "EBikeRide": "ride",
    "EMountainBikeRide": "ride", "VirtualRide": "ride",
    "NordicSki": "ski", "BackcountrySki": "ski", "RollerSki": "ski",
    "Walk": "walk", "Hike": "walk",
    "Kayaking": "paddle", "Canoeing": "paddle", "StandUpPaddling": "paddle", "Rowing": "paddle",
}
MATCH_START_DIFF = 10 * 60
MATCH_OVERLAP = 0.5


class RateLimited(Exception):
    pass


def request(method, url, token=None, data=None, headers=None):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(e.headers.get("X-RateLimit-Usage", ""))
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"message": body.decode(errors="replace")}


def authorize(client_id, client_secret):
    params = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "approval_prompt": "force",
        "redirect_uri": f"http://localhost:{REDIRECT_PORT}/", "scope": "activity:write,activity:read_all",
    })
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Klart – du kan stänga fliken.".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    url = f"https://www.strava.com/oauth/authorize?{params}"
    print("Öppnar webbläsaren för att godkänna åtkomst:\n ", url)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if "error" in result:
        sys.exit(f"Godkännande nekades: {result['error']}")
    if "activity:write" not in result.get("scope", ""):
        sys.exit("Behörigheten 'activity:write' godkändes inte – kör om och kryssa i den.")
    status, tok = request("POST", "https://www.strava.com/oauth/token", data={
        "client_id": client_id, "client_secret": client_secret,
        "code": result["code"], "grant_type": "authorization_code"})
    if status != 200:
        sys.exit(f"Kunde inte hämta token: {tok}")
    tok.update(client_id=client_id, client_secret=client_secret)
    TOKEN_FILE.write_text(json.dumps(tok, indent=1))
    return tok


def access_token(tok):
    if tok["expires_at"] - 120 < time.time():
        status, new = request("POST", "https://www.strava.com/oauth/token", data={
            "client_id": tok["client_id"], "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if status != 200:
            sys.exit(f"Kunde inte förnya token: {new}")
        tok.update(new)
        TOKEN_FILE.write_text(json.dumps(tok, indent=1))
    return tok["access_token"]


def multipart(fields, file_field, path):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                 f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    parts.append(path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_file(tok, act):
    body, ctype = multipart({
        "data_type": "fit", "name": act["name"], "description": act["description"],
        "trainer": "1" if act["trainer"] else "0", "external_id": act["id"],
    }, "file", HERE / "fit" / act["file"])
    status, up = request("POST", f"{API}/uploads", access_token(tok), body, {"Content-Type": ctype})
    if status >= 400:
        return {"status": "error", "error": up}
    for _ in range(30):
        if up.get("activity_id"):
            break
        if up.get("error"):
            err = up["error"]
            if "duplicate" in err.lower():
                return {"status": "duplicate", "error": err}
            return {"status": "error", "error": err}
        time.sleep(3)
        _, up = request("GET", f"{API}/uploads/{up['id']}", access_token(tok))
    else:
        return {"status": "pending", "upload_id": up.get("id")}
    aid = up["activity_id"]
    if act["sport_type"] in FIX_SPORT:
        request("PUT", f"{API}/activities/{aid}", access_token(tok), {"sport_type": act["sport_type"]})
    return {"status": "done", "activity_id": aid}


def create_manual(tok, act):
    if act["elapsed_time"] <= 0:
        return {"status": "skipped", "error": "ingen tid angiven"}
    status, res = request("POST", f"{API}/activities", access_token(tok), {
        "name": act["name"], "description": act["description"], "sport_type": act["sport_type"],
        "start_date_local": act["start_local"], "elapsed_time": act["elapsed_time"],
        "distance": act["distance"], "trainer": 1 if act["trainer"] else 0})
    if status >= 400:
        return {"status": "error", "error": res}
    return {"status": "done", "activity_id": res["id"]}


def utc_seconds(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def with_rate_limit(fn, *args):
    while True:
        try:
            return fn(*args)
        except RateLimited as e:
            wait = 15 * 60 - time.time() % (15 * 60) + 10
            print(f"  Rate limit nådd (användning {e}); väntar {wait / 60:.0f} min ...", flush=True)
            time.sleep(wait)


def fetch_existing(tok):
    """Hämtar alla aktiviteter som redan finns på Strava-kontot."""
    existing, page = [], 1
    while True:
        status, acts = with_rate_limit(request, "GET", f"{API}/athlete/activities?per_page=200&page={page}",
                                       access_token(tok))
        if status != 200:
            sys.exit(f"Kunde inte hämta befintliga aktiviteter: {acts}")
        if not acts:
            break
        existing += [{"id": a["id"], "name": a["name"], "sport_type": a.get("sport_type") or a.get("type"),
                      "start": utc_seconds(a["start_date"]), "elapsed": a.get("elapsed_time") or 0,
                      "date_local": a["start_date_local"][:10]} for a in acts]
        print(f"  hämtat {len(existing)} befintliga aktiviteter ...", flush=True)
        page += 1
    existing.sort(key=lambda e: e["start"])
    EXISTING_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    return existing


def find_existing(act, existing, starts):
    """Returnerar en befintlig Strava-aktivitet som motsvarar act, annars None."""
    family = SPORT_FAMILY.get(act["sport_type"], act["sport_type"])
    if not act["file"] and act["start_local"].endswith("T00:00:00"):
        # Bara datum känt: samma dag och samma sorts sport.
        day = act["start_local"][:10]
        for e in existing:
            if e["date_local"] == day and SPORT_FAMILY.get(e["sport_type"], e["sport_type"]) == family:
                return e
        return None
    start = utc_seconds(act["start_utc"])
    end = start + max(act["elapsed_time"], 60)
    # Kandidater: allt som startar upp till ett dygn före och fram till aktivitetens slut.
    lo = bisect.bisect_left(starts, start - 86400)
    hi = bisect.bisect_right(starts, end + MATCH_START_DIFF)
    for e in existing[lo:hi]:
        e_end = e["start"] + max(e["elapsed"], 60)
        if abs(e["start"] - start) <= MATCH_START_DIFF:
            return e
        overlap = min(end, e_end) - max(start, e["start"])
        if overlap >= MATCH_OVERLAP * min(end - start, e_end - e["start"]):
            return e
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-manual", action="store_true")
    ap.add_argument("--no-check", action="store_true")
    args = ap.parse_args()

    acts = json.loads((HERE / "activities.json").read_text(encoding="utf-8"))
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    todo = [a for a in acts if state.get(a["id"], {}).get("status") not in ("done", "duplicate", "skipped", "exists")]
    if args.no_manual:
        todo = [a for a in todo if a["file"]]

    if TOKEN_FILE.exists():
        tok = json.loads(TOKEN_FILE.read_text())
    elif args.client_id and args.client_secret:
        tok = authorize(args.client_id, args.client_secret)
    elif args.dry_run and args.no_check:
        tok = None
    else:
        sys.exit("Ange --client-id och --client-secret första gången (se instruktionerna överst i filen).")

    exists = []
    if not args.no_check:
        existing = fetch_existing(tok)
        starts = [e["start"] for e in existing]
        remaining = []
        for a in todo:
            match = find_existing(a, existing, starts)
            if match:
                exists.append((a, match))
            else:
                remaining.append(a)
        todo = remaining
        print(f"{len(exists)} aktiviteter finns redan på Strava och hoppas över.")
        if not args.dry_run:
            for a, e in exists:
                state[a["id"]] = {"status": "exists", "activity_id": e["id"], "strava_name": e["name"]}
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(acts)} aktiviteter totalt, {len(todo)} kvar att ladda upp i den här körningen.")
    if args.dry_run:
        for a, e in exists:
            print(f"  FINNS  {a['start_local']}  {a['name']}  ->  Strava {e['id']}: {e['name']}")
        for a in todo:
            print(f"  NY     {a['start_local']}  {a['sport_type']:16} {a['file'] or '(manuell)':32} {a['name']}")
        return

    for n, act in enumerate(todo, 1):
        res = with_rate_limit(upload_file if act["file"] else create_manual, tok, act)
        state[act["id"]] = res
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[{n}/{len(todo)}] {act['start_local']} {act['name']}: {res['status']}"
              + (f" – {res.get('error')}" if res.get("error") else ""), flush=True)

    summary = {}
    for s in state.values():
        summary[s["status"]] = summary.get(s["status"], 0) + 1
    print("Sammanställning:", summary)


if __name__ == "__main__":
    main()
