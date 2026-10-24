"""Konverterar en SportTracks 3 logbook (.logbook3) till FIT-filer för Strava.

Användning:
    python st3_to_fit.py "c:/Temp/Example.logbook3" utdata-mapp [--include-friends]

Skapar:
    utdata-mapp/fit/<datum>_<id>.fit   en fil per aktivitet med spårdata
    utdata-mapp/activities.json       metadata (namn, anteckningar, sporttyp) för upload_strava.py
    utdata-mapp/activities.csv        samma sak som tabell, för översikt

Format för TrackData version 4 (reverse-engineerat):
    GPSRoute:  block av [int64 sekunder sedan 1990-01-01][uint8 antal]
               följt av antal * [uint8 sekundoffset][float32 lat][float32 lon][float32 höjd]
    Övriga:    [uint8 värdetyp (1=uint8, 3=float32)] sedan block av
               [int64 tid][uint8 antal][uint8 läge] där läge 0 = varje värde föregås av
               uint8 sekundoffset, läge n>0 = värden med n sekunders intervall.
    Spåret avslutas med 8 nollbytes.
"""
import argparse
import base64
import csv
import datetime as dt
import json
import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{urn:uuid:D0EB2ED5-49B6-44e3-B13C-CF15BE7DD7DD}"
UTC = dt.timezone.utc
ST_EPOCH = dt.datetime(1990, 1, 1, tzinfo=UTC)
FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=UTC)

# (FIT sport, FIT sub_sport, Strava sport_type, trainer) per kategorisökväg.
# Den mest specifika matchningen vinner.
SPORT_MAP = {
    "Löpning": (1, 0, "Run", False),
    "Löpning/Stig": (1, 3, "TrailRun", False),
    "Orientering": (1, 0, "Run", False),
    "Orientering/MTB-O": (2, 8, "MountainBikeRide", False),
    "Cykel": (2, 0, "Ride", False),
    "Cykel/Väg": (2, 7, "Ride", False),
    "Cykel/Stig": (2, 8, "MountainBikeRide", False),
    "Cykel/Terräng": (2, 8, "MountainBikeRide", False),
    "Cykel/Fatbike": (2, 8, "MountainBikeRide", False),
    "Cykel/Träningscykel": (2, 6, "Ride", True),
    "Golf": (25, 0, "Golf", False),
    "Gång": (11, 0, "Walk", False),
    "Gång/Fjäll": (17, 0, "Hike", False),
    "Gång/Stig": (17, 0, "Hike", False),
    "Gång/Snösko": (35, 0, "Snowshoe", False),
    "Kanot": (19, 0, "Canoeing", False),
    "Kanot/Kajak": (41, 0, "Kayaking", False),
    "Kanot/Roddmaskin": (15, 14, "Rowing", True),
    "Längdåkning": (12, 0, "NordicSki", False),
    "Simning": (5, 0, "Swim", False),
    "Annat": (0, 0, "Workout", False),
    "Annat/Fjällvandring": (17, 0, "Hike", False),
    "Annat/Promenad": (11, 0, "Walk", False),
    "Annat/Slalom": (13, 0, "AlpineSki", False),
}
DEFAULT_SPORT = (0, 0, "Workout", False)


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def decode_gps(b):
    pts, i = [], 0
    while i + 8 < len(b):
        t, n = struct.unpack_from("<qB", b, i)
        i += 9
        for _ in range(n):
            off = b[i]
            lat, lon, ele = struct.unpack_from("<fff", b, i + 1)
            i += 13
            pts.append((t + off, lat, lon, ele))
    return pts


def decode_track(b):
    vtype, i = b[0], 1
    fmt, size = {1: ("<B", 1), 3: ("<f", 4)}[vtype]
    pts = []
    while i + 8 < len(b):
        t, n, mode = struct.unpack_from("<qBB", b, i)
        i += 10
        for k in range(n):
            if mode == 0:
                off = b[i]
                i += 1
            else:
                off = k * mode
            pts.append((t + off, struct.unpack_from(fmt, b, i)[0]))
            i += size
    return pts


def track_data(act, name):
    td = act.find(f"{NS}{name}/{NS}TrackData")
    return base64.b64decode(td.text) if td is not None and td.text else None


def ok(x):
    return x is not None and not math.isnan(x) and not math.isinf(x)


class FitWriter:
    """Minimal FIT-kodare: file_id, event, record, lap, session, activity."""

    BASE = {"enum": (0x00, "B"), "uint8": (0x02, "B"), "uint16": (0x84, "H"),
            "sint32": (0x85, "i"), "uint32": (0x86, "I"), "uint32z": (0x8C, "I")}
    INVALID = {"enum": 0xFF, "uint8": 0xFF, "uint16": 0xFFFF, "sint32": 0x7FFFFFFF,
               "uint32": 0xFFFFFFFF, "uint32z": 0}

    def __init__(self):
        self.data = bytearray()
        self.defs = {}

    def message(self, global_num, fields):
        """fields: lista av (fältnummer, bastyp, värde eller None)."""
        key = (global_num, tuple((f, t) for f, t, _ in fields))
        if key not in self.defs:
            local = len(self.defs) % 16
            self.defs = {k: v for k, v in self.defs.items() if v != local}
            self.defs[key] = local
            self.data += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
            for f, t, _ in fields:
                code, fmt = self.BASE[t]
                self.data += struct.pack("<BBB", f, struct.calcsize(fmt), code)
        self.data.append(self.defs[key])
        for f, t, v in fields:
            fmt = self.BASE[t][1]
            self.data += struct.pack("<" + fmt, self.INVALID[t] if v is None else v)

    @staticmethod
    def crc(data, crc=0):
        table = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
                 0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]
        for byte in data:
            tmp = table[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ table[byte & 0xF]
            tmp = table[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
        return crc

    def bytes(self):
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self.data), b".FIT")
        header += struct.pack("<H", self.crc(header))
        body = header + self.data
        return body + struct.pack("<H", self.crc(body))


def fit_ts(t):
    return int((t - FIT_EPOCH).total_seconds())


def semicircles(deg):
    return int(round(deg * (2 ** 31) / 180.0))


def scaled(x, scale, offset=0, maxv=0xFFFFFFFE):
    if not ok(x):
        return None
    v = int(round((x + offset) * scale))
    return v if 0 <= v <= maxv else None


def build_fit(act, start, sport):
    """Returnerar FIT-bytes, eller None om aktiviteten saknar spårdata."""
    streams = {}
    gps_b = track_data(act, "GPSRoute")
    if gps_b:
        streams["gps"] = [(t, (lat, lon, ele)) for t, lat, lon, ele in decode_gps(gps_b)]
    for key, name in (("hr", "HeartRateTrack"), ("dist", "DistanceTrack"), ("cad", "CadenceTrack")):
        b = track_data(act, name)
        if b:
            streams[key] = decode_track(b)
    streams = {k: v for k, v in streams.items() if v}
    if not streams:
        return None

    # Några få spår har en annan tidsbas än aktiviteten; flytta dem i så fall till starttiden.
    for key, pts in streams.items():
        first = ST_EPOCH + dt.timedelta(seconds=pts[0][0]) if abs(pts[0][0]) < 1e10 else None
        if first is None or abs((first - start).total_seconds()) > 2 * 86400:
            shift = pts[0][0] - int((start - ST_EPOCH).total_seconds())
            streams[key] = [(t - shift, v) for t, v in pts]

    # Slå ihop alla strömmar per sekund (senaste värdet vinner vid dubbletter).
    merged = {}
    for key, pts in streams.items():
        for t, v in pts:
            merged.setdefault(t, {})[key] = v
    times = sorted(merged)
    to_dt = lambda s: ST_EPOCH + dt.timedelta(seconds=s)
    t0, t1 = to_dt(times[0]), to_dt(times[-1])

    a = act.attrib
    total_time = float(a.get("totalTime") or 0) or (t1 - t0).total_seconds()
    total_dist = float(a.get("totalDistance") or 0)
    fit_sport, sub_sport = sport[0], sport[1]
    tz_offset = float(a.get("timeZoneUtcOffset") or 0) * 3600

    w = FitWriter()
    w.message(0, [(0, "enum", 4), (1, "uint16", 255), (2, "uint16", 0),
                  (3, "uint32z", 1), (4, "uint32", fit_ts(t0))])
    w.message(21, [(253, "uint32", fit_ts(t0)), (0, "enum", 0), (1, "enum", 0)])

    hr_vals = []
    for s in times:
        v = merged[s]
        lat = lon = alt = None
        if "gps" in v:
            la, lo, el = v["gps"]
            if ok(la) and ok(lo) and abs(la) <= 90 and abs(lo) <= 180 and (la, lo) != (0, 0):
                lat, lon = semicircles(la), semicircles(lo)
            alt = scaled(el, 5, 500, 0xFFFE)
        hr = v.get("hr")
        hr = int(round(hr)) if ok(hr) and 0 < hr < 255 else None
        if hr:
            hr_vals.append(hr)
        cad = v.get("cad")
        cad = int(round(cad)) if ok(cad) and 0 <= cad < 255 else None
        dist = scaled(v.get("dist"), 100)
        w.message(20, [(253, "uint32", fit_ts(to_dt(s))), (0, "sint32", lat), (1, "sint32", lon),
                       (2, "uint16", alt), (3, "uint8", hr), (4, "uint8", cad), (5, "uint32", dist)])

    end_ts = fit_ts(t1)
    w.message(21, [(253, "uint32", end_ts), (0, "enum", 0), (1, "enum", 4)])

    laps = [l.attrib for l in act.findall(f"{NS}Laps/{NS}Lap")]
    if not laps:
        laps = [{"startTime": t0.isoformat(), "totalTime": str(total_time), "totalDistance": str(total_dist)}]
    for i, lap in enumerate(laps):
        ls = parse_time(lap["startTime"])
        ltime = float(lap.get("totalTime") or 0)
        lap_end = laps[i + 1]["startTime"] if i + 1 < len(laps) else None
        lap_ts = fit_ts(parse_time(lap_end)) if lap_end else end_ts
        lhr = int(float(lap.get("avgHeartRate") or 0))
        w.message(19, [(253, "uint32", max(lap_ts, fit_ts(ls))), (0, "enum", 9), (1, "enum", 1),
                       (2, "uint32", fit_ts(ls)), (7, "uint32", scaled(ltime, 1000)),
                       (8, "uint32", scaled(ltime, 1000)),
                       (9, "uint32", scaled(float(lap.get("totalDistance") or 0), 100)),
                       (11, "uint16", scaled(float(lap.get("totalCalories") or "nan"), 1, 0, 0xFFFE)),
                       (15, "uint8", lhr if 0 < lhr < 255 else None)])

    avg_hr = int(round(sum(hr_vals) / len(hr_vals))) if hr_vals else None
    max_hr = max(hr_vals) if hr_vals else None
    w.message(18, [(253, "uint32", end_ts), (0, "enum", 8), (1, "enum", 1),
                   (2, "uint32", fit_ts(t0)), (5, "enum", fit_sport), (6, "enum", sub_sport),
                   (7, "uint32", scaled((t1 - t0).total_seconds(), 1000)),
                   (8, "uint32", scaled(total_time, 1000)),
                   (9, "uint32", scaled(total_dist, 100) if total_dist else None),
                   (11, "uint16", scaled(float(a.get("totalCalories") or "nan"), 1, 0, 0xFFFE)),
                   (16, "uint8", avg_hr), (17, "uint8", max_hr),
                   (22, "uint16", scaled(float(a.get("totalAscend") or "nan"), 1, 0, 0xFFFE)),
                   (23, "uint16", scaled(float(a.get("totalDescend") or "nan"), 1, 0, 0xFFFE)),
                   (25, "uint16", 0), (26, "uint16", len(laps))])
    w.message(34, [(253, "uint32", end_ts), (0, "uint32", scaled(total_time, 1000)),
                   (1, "uint16", 1), (2, "enum", 0), (3, "enum", 26), (4, "enum", 1),
                   (5, "uint32", end_ts + int(tz_offset))])
    return w.bytes(), len(times), "gps" in streams


def category_paths(root):
    """referenceId -> (sökväg under toppkategori, toppkategorinamn)."""
    paths = {}

    def walk(elem, parts, top):
        for c in elem:
            if c.tag in (NS + "Category", NS + "ActivityCategory"):
                name = c.get("name")
                p = parts + [name]
                t = top or name
                paths[c.get("referenceId")] = ("/".join(p[1:]), t)
                sub = c.find(NS + "Categories")
                if sub is not None:
                    walk(sub, p, t)

    walk(root, [], None)
    return paths


def sport_for(path):
    parts = path.split("/") if path else []
    while parts:
        key = "/".join(parts)
        if key in SPORT_MAP:
            return SPORT_MAP[key]
        parts.pop()
    return DEFAULT_SPORT


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logbook")
    ap.add_argument("outdir")
    ap.add_argument("--include-friends", action="store_true",
                    help="ta med aktiviteter under 'Mina vänners aktiviteter' (inkl. Bil)")
    args = ap.parse_args()

    out = Path(args.outdir)
    (out / "fit").mkdir(parents=True, exist_ok=True)

    # Kategorierna ligger efter aktiviteterna i filen, så läs dem i ett första pass.
    cats = {}
    for _, el in ET.iterparse(args.logbook):
        if el.tag == NS + "ActivityCategories":
            cats = category_paths(el)
            break
        if el.tag == NS + "Activity":
            el.clear()

    rows, skipped = [], {"friends": 0, "no_start": 0}
    for _, el in ET.iterparse(args.logbook):
        if el.tag != NS + "Activity" or el.find(NS + "Metadata") is None:
            continue
        a = el.attrib
        path, top = cats.get(a.get("categoryId"), (a.get("categoryName", ""), ""))
        if top == "Mina vänners aktiviteter" and not args.include_friends:
            skipped["friends"] += 1
            el.clear()
            continue
        if not a.get("startTime"):
            skipped["no_start"] += 1
            el.clear()
            continue
        start = parse_time(a["startTime"])
        sport = sport_for(path)
        local = start + dt.timedelta(hours=float(a.get("timeZoneUtcOffset") or 0))
        title = a.get("name") or (path.replace("/", " – ") if path else "Aktivitet")
        notes = (a.get("notes") or "").strip()
        if a.get("location"):
            notes = (f"Plats: {a['location']}\n" + notes).strip()

        rid = a["referenceId"]
        fname = f"{local:%Y-%m-%d_%H%M}_{rid[:8]}.fit"
        fit = build_fit(el, start, sport)
        if fit:
            data, npts, has_gps = fit
            (out / "fit" / fname).write_bytes(data)
        else:
            fname, npts, has_gps = None, 0, False
        rows.append({
            "id": rid, "file": fname, "name": title, "description": notes,
            "category": path, "sport_type": sport[2], "trainer": sport[3] or not has_gps,
            "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "start_local": local.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_time": int(float(a.get("totalTime") or 0)),
            "distance": float(a.get("totalDistance") or 0), "points": npts, "gps": has_gps,
        })
        el.clear()

    rows.sort(key=lambda r: r["start_utc"])
    (out / "activities.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out / "activities.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        wr.writeheader()
        wr.writerows(rows)

    with_file = sum(1 for r in rows if r["file"])
    print(f"{len(rows)} aktiviteter: {with_file} FIT-filer ({sum(r['gps'] for r in rows)} med GPS), "
          f"{len(rows) - with_file} utan spårdata (manuella). Hoppade över: {skipped}")


if __name__ == "__main__":
    sys.exit(main())
