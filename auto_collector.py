# ============================================================
# auto_collector.py
# Fully automated daily match collector.
# Run once per day — reads today's matches from group_schedule.py,
# sleeps until 5 minutes before each kickoff, discovers live
# streams via hybrid search + playlist method, and collects
# chat data for all found streams using staggered A/B/C polling.
# Required env vars: YOUTUBE_API_KEY, FOOTBALL_DATA_API_KEY
# ============================================================

import sys
import time
import threading
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from googleapiclient.discovery import build

sys.stdout.reconfigure(encoding='utf-8')

from channels import CHANNELS
from group_schedule import MATCHES_SCHEDULE
from scorer import score_batch
from database import insert_message, insert_stream_stats

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")

MAX_MATCH_MINUTES = 150
MAX_SEARCH_CALLS = 5

# ADT = UTC-3, used only for display in _fmt_now()
_ADT = timedelta(hours=3)

_SAVED_IDS   = {ch["id"] for ch in CHANNELS}
_SAVED_NAMES = {ch["id"]: ch["name"] for ch in CHANNELS}


# ── time helpers ────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_now() -> str:
    return (_now_utc() - _ADT).strftime("%Y-%m-%d %H:%M ADT")


def _kickoff_utc(match: dict) -> datetime:
    return datetime.fromisoformat(match["kickoff_utc_iso"].replace("Z", "+00:00"))


# ── schedule helpers ────────────────────────────────────────────────────────────

def _today_matches() -> list:
    """Return today's matches by ADT date (the 'date' field), including any in progress."""
    now = datetime.now(timezone.utc)
    today_adt = (now - _ADT).date()
    matches = [
        m for m in MATCHES_SCHEDULE
        if (
            datetime.strptime(m["date"], "%Y-%m-%d").date() == today_adt
            or (_kickoff_utc(m) <= now < _kickoff_utc(m) + timedelta(minutes=MAX_MATCH_MINUTES))
        )
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

def _classify_event(m: dict):
    """
    Return (text, message_type) for a usable chat event, or None to skip.
    Default path (textMessageEvent) is unchanged from before; superchat/membership
    events are additionally captured now instead of being dropped.
    """
    snippet = m.get("snippet", {}) or {}
    event_type = snippet.get("type")
    text = snippet.get("displayMessage")

    if event_type == "textMessageEvent":
        return (text, "text") if text else None

    if event_type == "superChatEvent":
        if not text:
            text = snippet.get("superChatDetails", {}).get("amountDisplayString")
        return (text, "superchat") if text else None

    if event_type in ("newSponsorEvent", "memberMilestoneChatEvent", "membershipGiftingEvent"):
        return (text, "membership") if text else None

    return None


def _superchat_amount(m: dict):
    return m.get("snippet", {}).get("superChatDetails", {}).get("amountDisplayString")


def collect_stream_staggered(video_id: str, match_id: str,
                              match_minute_tracker: list, poll_interval: int = 180):
    yt = build("youtube", "v3", developerKey=API_KEY)

    # Wait until stream has an active live chat
    while True:
        resp = yt.videos().list(
            part="liveStreamingDetails,snippet",
            id=video_id
        ).execute()
        item = resp["items"][0]
        details = item.get("liveStreamingDetails", {})
        if "activeLiveChatId" in details:
            live_chat_id = details["activeLiveChatId"]
            stream_title = f"{item['snippet']['channelTitle']} — {item['snippet']['title']}"
            break
        print(f"[{video_id}] Stream not live yet — retrying in 10 mins")
        time.sleep(600)

    print(f"[{video_id}] Connected: {stream_title}")

    def _execute_with_retry(request, max_retries=5):
        for attempt in range(max_retries):
            try:
                return request.execute()
            except (ConnectionAbortedError, ConnectionResetError, OSError) as e:
                if attempt == max_retries - 1:
                    raise
                wait = 30 * (attempt + 1)
                print(f"[{video_id}] Network error ({e}), retrying in {wait}s...")
                time.sleep(wait)

    # Wait for first messages
    next_page_token = None
    while True:
        response = _execute_with_retry(
            yt.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=next_page_token
            )
        )
        messages = [
            m for m in response.get("items", [])
            if m.get("snippet", {}).get("type") == "textMessageEvent"
        ]
        if messages:
            break
        print(f"[{video_id}] No messages yet — checking again in 10 mins")
        time.sleep(600)

    # Main collection loop
    while True:
        response = _execute_with_retry(
            yt.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                pageToken=next_page_token
            )
        )
        events = []
        for m in response.get("items", []):
            classified = _classify_event(m)
            if classified:
                events.append((m, classified[0], classified[1]))

        if events:
            texts  = [text for _, text, _ in events]
            scores = score_batch(texts)
            for (m, text, msg_type), score in zip(events, scores):
                snippet = m.get("snippet", {})
                author_details = m.get("authorDetails", {})
                insert_message(
                    match_id          = match_id,
                    source            = "youtube",
                    stream_id         = video_id,
                    stream_title      = stream_title,
                    timestamp         = datetime.now(timezone.utc).isoformat(),
                    message_timestamp = snippet.get("publishedAt"),
                    match_minute      = match_minute_tracker[0],
                    author            = author_details.get("displayName", "unknown"),
                    text              = text,
                    scores            = score,
                    message_type      = msg_type,
                    superchat_amount  = _superchat_amount(m) if msg_type == "superchat" else None
                )
        next_page_token = response.get("nextPageToken")
        print(f"[{video_id}] Fetched {len(events)} messages — waiting {poll_interval}s")
        time.sleep(poll_interval)


def _collect_with_delay(video_id: str, match_id: str, tracker: list,
                        delay: int, poll_interval: int = 180):
    if delay > 0:
        time.sleep(delay)
    collect_stream_staggered(video_id, match_id, tracker, poll_interval)


def _pending_checker(youtube_key: str, pending: list, match_id: str,
                     tracker: list, connected_ids: set,
                     group_delay_map: dict, lock: threading.Lock,
                     stop_event: threading.Event):
    """
    Background thread: every 10 minutes checks pending streams via videos.list (not search).
    When a stream goes live, spins up a collect_stream_staggered thread in its pre-assigned group.
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
                                args=(p["video_id"], match_id, tracker, delay, 180),
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


def _stream_stats_poller(youtube_key: str, match_id: str, connected_ids: set,
                          lock: threading.Lock, stop_event: threading.Event,
                          poll_interval: int = 180):
    """
    Background thread: once per poll cycle, fetches concurrentViewers for ALL
    currently active streams in ONE videos.list call (1 quota unit total,
    regardless of stream count) and logs one stream_stats row per stream.
    Fail-safe: any error here skips the stats row and never touches chat polling.
    """
    yt = build("youtube", "v3", developerKey=youtube_key)

    while not stop_event.is_set():
        time.sleep(poll_interval)
        if stop_event.is_set():
            break

        with lock:
            ids = list(connected_ids)
        if not ids:
            continue

        try:
            resp = yt.videos().list(
                part="liveStreamingDetails",
                id=",".join(ids)
            ).execute()
        except Exception as e:
            print(f"[STATS ERR] {e}")
            continue

        now_iso = datetime.now(timezone.utc).isoformat()
        for item in resp.get("items", []):
            try:
                viewers = int(
                    item.get("liveStreamingDetails", {}).get("concurrentViewers", 0) or 0
                )
            except (ValueError, TypeError):
                viewers = 0
            try:
                insert_stream_stats(
                    match_id=match_id,
                    stream_id=item.get("id"),
                    timestamp=now_iso,
                    concurrent_viewers=viewers
                )
            except Exception as e:
                print(f"[STATS DB ERR] {e}")

    print("[STATS] Poller thread exiting")


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
            args=(stream["video_id"], match_id, match_minute_tracker, delay, 180),
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

    # Background viewer-stats poller (batched, 1 quota unit per cycle regardless of stream count)
    st_thread = threading.Thread(
        target=_stream_stats_poller,
        args=(API_KEY, match_id, connected_ids, lock, stop_event, 180),
        daemon=True
    )
    st_thread.start()

    # Tick match minute — stop at minute 150 or when next match is close
    while True:
        # Hard stop at minute 150
        if match_minute_tracker[0] >= MAX_MATCH_MINUTES:
            print(f"[{label}] Minute {MAX_MATCH_MINUTES} reached — stopping")
            stop_event.set()
            return

        # Yield to next match if it's starting soon
        if next_kickoff:
            secs_to_next = (next_kickoff - _now_utc()).total_seconds()
            if secs_to_next <= 300:
                print(f"[{label}] Next match in {int(secs_to_next)}s — stopping")
                stop_event.set()
                return

        time.sleep(60)
        match_minute_tracker[0] += 1
        print(f"Match minute: {match_minute_tracker[0]}")


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
            print(f"  [SKIP] {label} — already finished")
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
