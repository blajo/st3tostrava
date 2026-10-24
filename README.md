# SportTracks 3 → Strava

Tools for moving the training history in a SportTracks 3.1 logbook (`.logbook3`) to Strava.

- `st3_to_fit.py` converts the logbook into one FIT file per activity and writes the activity metadata to a JSON file.
- `upload_strava.py` uploads the activities through the Strava API. It skips activities that are already on Strava and resumes where it stopped.

Both scripts need Python 3.9 or later and use only the standard library.

## 1. Convert the logbook

```bash
python st3_to_fit.py "C:/Temp/Example.logbook3" .
```

The converter writes the following files to the output directory (here `.`):

| File | Contents |
|---|---|
| `fit/<date>_<time>_<id>.fit` | One FIT file for each activity that has track data: GPS, heart rate, distance, cadence, laps, calories and ascent/descent. |
| `activities.json` | Name, notes, sport type, start time, duration and distance for every activity. `upload_strava.py` reads this file. |
| `activities.csv` | The same data as a semicolon-separated table. |

Activities that have only summary data (no GPS or heart-rate track) get no FIT file. The upload script creates them as manual activities instead.

Activities under the top-level category **"Mina vänners aktiviteter"** (friends' activities, including the *Bil* car category) are skipped by default. Pass `--include-friends` to convert them too.

### Sport types

The sport type comes from the SportTracks category. The most specific match in `SPORT_MAP` in `st3_to_fit.py` wins:

| SportTracks category | Strava sport type |
|---|---|
| Löpning, Orientering | Run |
| Löpning/Stig | Trail Run |
| Cykel, Cykel/Väg | Ride |
| Cykel/Stig, Terräng, Fatbike, Orientering/MTB-O | Mountain Bike Ride |
| Cykel/Träningscykel | Ride (indoor) |
| Gång, Annat/Promenad | Walk |
| Gång/Fjäll, Gång/Stig, Annat/Fjällvandring | Hike |
| Gång/Snösko | Snowshoe |
| Längdåkning | Nordic Ski |
| Annat/Slalom | Alpine Ski |
| Kanot, Kanadensare | Canoeing |
| Kanot/Kajak | Kayaking |
| Kanot/Roddmaskin | Rowing (indoor) |
| Golf | Golf |
| Simning | Swim |
| Everything else (e.g. Innebandy) | Workout |

## 2. Upload to Strava

### One-time setup

1. Create an API application at <https://www.strava.com/settings/api>. Set **Authorization Callback Domain** to `localhost`.
2. Note the **Client ID** and **Client Secret**.

### Dry run

```bash
python upload_strava.py --client-id YOUR_ID --client-secret YOUR_SECRET --dry-run
```

The first run opens a browser window so you can authorize the app. Approve the access the app asks for. The token is saved in `strava_token.json` and refreshed automatically, so you don't need the client ID or secret again.

A dry run changes nothing. It lists each activity as:

- `FINNS`: already on Strava, with the Strava activity it matched
- `NY`: will be uploaded

### Real upload

Start with a few activities and check them on Strava:

```bash
python upload_strava.py --limit 5
```

When they look right, upload the rest:

```bash
python upload_strava.py
```

### Options

| Flag | Effect |
|---|---|
| `--dry-run` | Show what would happen without uploading anything. |
| `--limit N` | Upload at most N activities in this run. |
| `--no-manual` | Skip activities without track data instead of creating manual activities. |
| `--no-check` | Don't check for activities that already exist on Strava. |

### Duplicate check

Before uploading, the script downloads all activities on your Strava account into `strava_existing.json`. An activity from the logbook counts as already on Strava if one of the following holds:

- it starts within ±10 minutes of an existing activity, or
- at least half of its duration overlaps an existing activity.

Activities that have only a date (start time 00:00) match an existing activity on the same day with the same kind of sport. All running types count as one kind, and so do all cycling types.

The thresholds are `MATCH_START_DIFF` and `MATCH_OVERLAP` at the top of `upload_strava.py`.

### Rate limits and resuming

Strava limits API requests per 15 minutes and per day. Uploading a large history can therefore take several days. When the script hits the limit, it waits and then continues on its own. Keep the terminal open and stop the computer from going to sleep.

You can stop the script at any time (Ctrl+C) and run it again. Progress is saved in `upload_state.json`, one entry per activity:

| Status | Meaning |
|---|---|
| `done` | Uploaded (includes the Strava activity ID). |
| `exists` | Already on Strava. |
| `duplicate` | Rejected by Strava as a duplicate. |
| `skipped` | Manual activity without a duration; not uploaded. |
| `error` | Failed. The script retries it on the next run. |
| `pending` | Strava hadn't finished processing the upload. The script uploads it again on the next run. |

> **Keep `strava_token.json` private.** It contains your client secret and gives write access to your Strava account. Delete it when you're done.

## The `.logbook3` format

A SportTracks 3 logbook is a UTF-8 XML file. Each `<Activity>` element holds its summary as attributes. Tracks are stored in base64-encoded `<TrackData version="4">` elements, using the binary format below. The format isn't officially documented; this description was reverse-engineered for the converter.

**Times** are `int64` seconds since 1990-01-01 UTC. A few old activities use a different time base. The converter moves their tracks to the activity's start time.

**`GPSRoute`** is a sequence of blocks:

```
int64  block start time
uint8  number of points (n)
n × {  uint8 second offset from block start
       float32 latitude, float32 longitude, float32 elevation (m)  }
```

**`HeartRateTrack`, `DistanceTrack`, `CadenceTrack`** start with one value-type byte (`1` = uint8, `3` = float32), followed by blocks:

```
int64  block start time
uint8  number of values (n)
uint8  mode
mode 0:  n × { uint8 second offset, value }
mode k:  n × value, k seconds apart
```

Every track ends with 8 zero bytes. Distance values are cumulative metres.
