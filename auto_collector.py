# ============================================================
# auto_collector.py
# Fully automated daily match collector.
# Run once per day — reads today's matches from matches.py,
# sleeps until 5 minutes before each kickoff, discovers live
# streams via hybrid search + playlist method, and collects
# chat data for all found streams using staggered A/B/C polling.
# ============================================================

import time
import threading
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from googleapiclient.discovery import build

from channels import CHANNELS
from matches import MATCHES_SCHEDULE
from youtube_collector import collect_stream

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")

MAX_MATCH_MINUTES = 120
MAX_SEARCH_CALLS = 5

# Kickoff times in matches.py are UTC. ADT = UTC-3.
# To get "today in ADT": subtract 3h from now_utc and take .date()
_ADT = timedelta(hours=3)

_SAVED_IDS   = {ch["id"] for ch in CHANNELS}
_SAVED_NAMES = {ch["id"]: ch["name"] for ch in CHANNELS}


# ── time helpers ────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_now() -> str:
    return (_now_utc() - _ADT).strftime("%Y-%m-%d %H:%M ADT")


def _kickoff_utc(match: dict) -> datetime:
    return datetime.fromisoformat(match["kickoff_utc"].replace("Z", "+00:00"))


# ── schedule helpers ────────────────────────────────────────────────────────────

def _today_matches() -> list:
    """Return today's matches (by ADT date), sorted by kickoff ascending."""
    today_adt = (_now_utc() - _ADT).date()
    matches = [
        m for m in MATCHES_SCHEDULE
        if (_kickoff_utc(m) - _ADT).date() == today_adt
    ]
    return sorted(matches, key=_kickoff_utc)


# ── stream discovery ────────────────────────────────────────────────────────────

def _global_search(youtube, match: dict, search_counter: list) -> list:
    """
    Step 1+2: Global search for the match → filter to live → top 10 by viewers.
    Costs 1 search call + 1 videos.list unit.
    Returns list of stream dicts.
    """
    if search_counter[0] >= MAX_SEARCH_CALLS:
        print(f"[SEARCH] Cap reached ({MAX_SEARCH_CALLS}) — skipping")
        return []

    q = f"{match['team_1']} {match['team_2']} world cup watchalong"
    try:
        resp = youtube.search().list(
            part="snippet",
            q=q,
            type="video",
            maxResults=50,
            order="relevance"
        ).execute()
        search_counter[0] += 1
        if search_counter[0] == MAX_SEARCH_CALLS - 1:
            print(f"[SEARCH] Warning: {search_counter[0]}/{MAX_SEARCH_CALLS} search calls used")
    except Exception as e:
        print(f"[SEARCH ERR] {e}")
        return []

    video_ids = [
        item["id"]["videoId"]
        for item in resp.get("items", [])
        if item["id"].get("videoId")
    ]
    if not video_ids:
        return []

    try:
        vids = youtube.videos().list(
            part="snippet,liveStreamingDetails,statistics",
            id=",".join(video_ids)
        ).execute()
    except Exception as e:
        print(f"[SEARCH/videos ERR] {e}")
        return []

    live = []
    for item in vids.get("items", []):
        if item["snippet"].get("liveBroadcastContent") != "live":
            continue
        channel_id = item["snippet"]["channelId"]
        viewers = 0
        try:
            viewers = int(item.get("liveStreamingDetails", {}).get("concurrentViewers", 0) or 0)
        except (ValueError, TypeError):
            pass
        live.append({
            "video_id":    item["id"],
            "title":       item["snippet"]["title"],
            "channel_name": _SAVED_NAMES.get(channel_id, item["snippet"]["channelTitle"]),
            "channel_id":  channel_id,
            "viewers":     viewers,
            "is_saved":    channel_id in _SAVED_IDS,
        })

    live.sort(key=lambda x: x["viewers"], reverse=True)
    return live[:10]


def _playlist_check_saved(youtube) -> tuple:
    """
    Step 3: Check all saved channels via uploads playlist.
    Uses the UU-prefix shortcut (saves 1 API unit vs channels.list per channel).
    Costs 2 units per channel: playlistItems.list + videos.list.
    Returns (live_streams, pending_streams, skipped_names).
    """
    live = []
    pending = []
    skipped = []

    for channel in CHANNELS:
        channel_id   = channel["id"]
        channel_name = channel["name"]
        playlist_id  = "UU" + channel_id[2:]  # uploads playlist shortcut

        try:
            pl = youtube.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=10
            ).execute()
        except Exception as e:
            print(f"  [playlist ERR] {channel_name}: {e}")
            skipped.append(channel_name)
            continue

        video_ids = [
            item["snippet"]["resourceId"]["videoId"]
            for item in pl.get("items", [])
            if "resourceId" in item.get("snippet", {})
        ]
        if not video_ids:
            skipped.append(channel_name)
            continue

        try:
            vids = youtube.videos().list(
                part="snippet,liveStreamingDetails,statistics",
                id=",".join(video_ids)
            ).execute()
        except Exception as e:
            print(f"  [videos ERR] {channel_name}: {e}")
            skipped.append(channel_name)
            continue

        found = False
        for item in vids.get("items", []):
            broadcast = item["snippet"].get("liveBroadcastContent", "none")
            if broadcast == "live":
                viewers = 0
                try:
                    viewers = int(
                        item.get("liveStreamingDetails", {}).get("concurrentViewers", 0) or 0
                    )
                except (ValueError, TypeError):
                    pass
                live.append({
                    "video_id":    item["id"],
                    "title":       item["snippet"]["title"],
                    "channel_name": channel_name,
                    "channel_id":  channel_id,
                    "viewers":     viewers,
                    "is_saved":    True,
                })
                found = True
                break
            elif broadcast == "upcoming":
                pending.append({
                    "video_id":    item["id"],
                    "channel_name": channel_name,
                    "channel_id":  channel_id,
                })
                found = True
                break

        if not found:
            skipped.append(channel_name)

    return live, pending, skipped


def discover_streams(youtube, match: dict, search_counter: list) -> tuple:
    """
    Discover live streams: global search (top 10) + all saved channels (playlist).
    Returns (live_streams, pending_streams).
    """
    label = f"{match['team_1'].upper()} VS {match['team_2'].upper()}"
    print(f"\n[{_fmt_now()}] {label} — Starting scan...")

    # Step 1+2: global search
    search_live = _global_search(youtube, match, search_counter)
    search_ids  = {s["video_id"] for s in search_live}

    # Step 3: playlist check all saved channels
    playlist_live, pending, skipped = _playlist_check_saved(youtube)

    # Step 4: merge (deduplicate by video_id; saved channels take precedence for name/flag)
    combined: dict = {s["video_id"]: s for s in search_live}
    for s in playlist_live:
        if s["video_id"] not in combined:
            combined[s["video_id"]] = s
        else:
            # already found via search — promote to saved
            combined[s["video_id"]]["is_saved"]    = True
            combined[s["video_id"]]["channel_name"] = s["channel_name"]

    live_streams = list(combined.values())

    # Summary
    for s in live_streams:
        tag     = "✅ saved" if s["is_saved"] else "➕ new"
        viewers = f"{s['viewers']:,}" if s["viewers"] else "?"
        print(f"[LIVE] {s['channel_name']} → {s['title'][:50]} | {viewers} viewers {tag}")
    for p in pending:
        print(f"[PENDING] {p['channel_name']} → scheduled, checking in 10 mins")
    for name in skipped:
        print(f"[SKIP] {name} → not live")

    return live_streams, pending


# ── collection helpers ──────────────────────────────────────────────────────────

def _collect_with_delay(video_id: str, match_id: str, tracker: list, delay: int):
    if delay > 0:
        time.sleep(delay)
    collect_stream(video_id, match_id, tracker)


def _pending_checker(youtube_key: str, pending: list, match_id: str,
                     tracker: list, connected_ids: set,
                     group_delay_map: dict, lock: threading.Lock,
                     stop_event: threading.Event):
    """
    Background thread: every 10 minutes checks pending streams via videos.list (not search).
    When a stream goes live, spins up a collect_stream thread in its pre-assigned group.
    """
    yt = build("youtube", "v3", developerKey=youtube_key)

    while not stop_event.is_set() and pending:
        time.sleep(600)
        if stop_event.is_set():
            break

        still_pending = []
        for p in list(pending):
            try:
                resp  = yt.videos().list(
                    part="snippet,liveStreamingDetails",
                    id=p["video_id"]
                ).execute()
                items = resp.get("items", [])
                if not items:
                    still_pending.append(p)
                    continue

                broadcast = items[0]["snippet"].get("liveBroadcastContent", "none")
                if broadcast == "live":
                    with lock:
                        if p["video_id"] not in connected_ids:
                            connected_ids.add(p["video_id"])
                            delay = group_delay_map.get(p["video_id"], 0)
                            print(f"[PENDING→LIVE] {p['channel_name']} → connecting now "
                                  f"(group delay {delay}s)")
                            t = threading.Thread(
                                target=_collect_with_delay,
                                args=(p["video_id"], match_id, tracker, delay),
                                daemon=True
                            )
                            t.start()
                elif broadcast == "upcoming":
                    still_pending.append(p)
                # "none" → stream ended or not found — drop it

            except Exception as e:
                print(f"[PENDING ERR] {p['channel_name']}: {e}")
                still_pending.append(p)

        pending.clear()
        pending.extend(still_pending)

    print("[PENDING] Checker thread exiting")


# ── match runner ────────────────────────────────────────────────────────────────

def run_match(youtube, match: dict, minute_start: int, next_kickoff):
    match_id = match["mode"]
    label    = f"{match['team_1'].upper()} VS {match['team_2'].upper()}"
    search_counter = [0]

    live_streams, pending = discover_streams(youtube, match, search_counter)

    # Retry until at least one stream (live or pending) is found
    while not live_streams and not pending:
        print(f"[{label}] No streams found — retrying in 10 minutes")
        time.sleep(600)
        live_streams, pending = discover_streams(youtube, match, search_counter)

    match_minute_tracker = [minute_start]
    connected_ids = {s["video_id"] for s in live_streams}
    lock = threading.Lock()

    # Assign all streams (live + pending) to A/B/C groups round-robin
    # Pending streams get a pre-assigned group delay for when they go live
    all_streams = (
        [(s["channel_name"], s["video_id"], False) for s in live_streams] +
        [(p["channel_name"], p["video_id"], True)  for p in pending]
    )

    group_names: list = [[], [], []]
    group_delay_map: dict = {}  # video_id → delay in seconds for pending streams

    for i, (name, vid, is_pending) in enumerate(all_streams):
        g     = i % 3
        delay = g * 60
        group_names[g].append(name + (" (pending)" if is_pending else ""))
        group_delay_map[vid] = delay

    group_labels = ["A", "B", "C"]
    for g_idx in range(3):
        if group_names[g_idx]:
            print(f"[GROUP {group_labels[g_idx]}] {', '.join(group_names[g_idx])}")

    # Spin up threads for live streams with staggered delays
    for stream in live_streams:
        delay = group_delay_map[stream["video_id"]]
        t = threading.Thread(
            target=_collect_with_delay,
            args=(stream["video_id"], match_id, match_minute_tracker, delay),
            daemon=True
        )
        t.start()

    # Background pending checker
    stop_event = threading.Event()
    if pending:
        pending_list = list(pending)
        pt = threading.Thread(
            target=_pending_checker,
            args=(API_KEY, pending_list, match_id, match_minute_tracker,
                  connected_ids, group_delay_map, lock, stop_event),
            daemon=True
        )
        pt.start()

    # Tick match minute — stop at cap or 5 minutes before next kickoff
    while match_minute_tracker[0] < MAX_MATCH_MINUTES:
        if next_kickoff:
            secs_to_next = (next_kickoff - _now_utc()).total_seconds()
            if secs_to_next <= 300:
                print(f"[{label}] Next match in {int(secs_to_next)}s — stopping collection")
                stop_event.set()
                return
        time.sleep(60)
        match_minute_tracker[0] += 1
        print(f"Match minute: {match_minute_tracker[0]}")

    stop_event.set()
    print(f"[{label}] Match complete after {MAX_MATCH_MINUTES} minutes")


# ── daily scheduler ─────────────────────────────────────────────────────────────

def run_daily_collector():
    youtube = build("youtube", "v3", developerKey=API_KEY)

    matches = _today_matches()
    now_utc = _now_utc()

    if not matches:
        today_label = (now_utc - _ADT).strftime("%Y-%m-%d")
        print(f"[{_fmt_now()}] No matches scheduled for {today_label} ADT. Exiting.")
        return

    print(f"[{_fmt_now()}] Today's schedule: {len(matches)} match(es)")

    # Classify each match: skip / in-progress / upcoming
    to_run = []
    for match in matches:
        kickoff     = _kickoff_utc(match)
        elapsed_min = (now_utc - kickoff).total_seconds() / 60
        label       = f"{match['team_1']} vs {match['team_2']}"

        if elapsed_min > MAX_MATCH_MINUTES:
            print(f"  [SKIP] {label} — ended {int(elapsed_min)}m ago")
        elif elapsed_min >= 0:
            minute_start = int(elapsed_min)
            print(f"  [IN PROGRESS] {label} at minute {minute_start} — joining immediately")
            to_run.append((kickoff, match, minute_start))
        else:
            print(f"  [UPCOMING] {label} — kicks off at {kickoff.strftime('%H:%M UTC')}")
            to_run.append((kickoff, match, 0))

    if not to_run:
        print("All matches today have ended. Exiting.")
        return

    for i, (kickoff, match, minute_start) in enumerate(to_run):
        label       = f"{match['team_1'].upper()} VS {match['team_2'].upper()}"
        next_kickoff = to_run[i + 1][0] if i + 1 < len(to_run) else None

        # Sleep until 5 minutes before kickoff (future matches only)
        elapsed_sec = (_now_utc() - kickoff).total_seconds()
        if elapsed_sec < 0:
            sleep_secs = abs(elapsed_sec) - 300
            wake_at    = kickoff - timedelta(seconds=300)
            if sleep_secs > 0:
                print(f"[{label}] Kickoff {kickoff.strftime('%H:%M UTC')} — "
                      f"sleeping until {wake_at.strftime('%H:%M UTC')}")
                time.sleep(sleep_secs)
            print(f"[{label}] Waking up — 5 minutes to kickoff")

        run_match(youtube, match, minute_start, next_kickoff)

    print(f"[{_fmt_now()}] All matches complete for today. Exiting.")


if __name__ == "__main__":
    run_daily_collector()
