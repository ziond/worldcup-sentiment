# ============================================
# youtube_collector.py
# Connects to a YouTube live stream chat.
# Polls for new messages every 5 seconds.
# Scores each message and saves to database.
# ============================================

import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import os
from googleapiclient.discovery import build

from scorer import score_batch
from database import insert_message

import threading

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")

def get_stream_info(youtube, video_id: str) -> dict:
    while True:
        response = youtube.videos().list(
            part="liveStreamingDetails,snippet",
            id=video_id
        ).execute()

        item = response["items"][0]
        details = item.get("liveStreamingDetails", {})

        if "activeLiveChatId" in details:
            return {
                "live_chat_id": details["activeLiveChatId"],
                "title":        item["snippet"]["title"],
                "channel":      item["snippet"]["channelTitle"]
            }

        print(f"[{video_id}] Stream not live yet — retrying in 10 mins")
        time.sleep(600)

def collect_stream(video_id: str, match_id: str, match_minute_tracker: list):
    youtube = build("youtube", "v3", developerKey=API_KEY)
    info = get_stream_info(youtube, video_id)
    live_chat_id = info["live_chat_id"]
    stream_title = f"{info['channel']} — {info['title']}"

    print(f"[{video_id}] Connected: {stream_title}")

    next_page_token = None

    while True:
        response = youtube.liveChatMessages().list(
            liveChatId=live_chat_id,
            part="snippet,authorDetails",
            pageToken=next_page_token
        ).execute()

        messages = [
            m for m in response.get("items", [])
            if m["snippet"]["type"] == "textMessageEvent"
        ]

        if messages:
            texts = [m["snippet"]["displayMessage"] for m in messages]
            scores = score_batch(texts)

            for m, score in zip(messages, scores):
                insert_message(
                    match_id          = match_id,
                    source            = "youtube",
                    stream_id         = video_id,
                    stream_title      = stream_title,
                    timestamp         = datetime.now(timezone.utc).isoformat(),
                    message_timestamp = m["snippet"]["publishedAt"],
                    match_minute      = match_minute_tracker[0],
                    author            = m["authorDetails"]["displayName"],
                    text              = m["snippet"]["displayMessage"],
                    scores            = score
                )

        next_page_token = response.get("nextPageToken")
        wait = response.get("pollingIntervalMillis", 5000) / 1000
        print(f"[{video_id}] Fetched {len(messages)} messages — waiting {wait}s")
        time.sleep(wait)

def collect(video_ids: list, match_id: str):
    match_minute_tracker = [1]  # shared list so threads can read it

    threads = []
    for video_id in video_ids:
        t = threading.Thread(target=collect_stream, args=(video_id, match_id, match_minute_tracker))
        t.daemon = True
        threads.append(t)
        t.start()

    # increment match minute every 60 seconds
    while True:
        time.sleep(60)
        match_minute_tracker[0] += 1
        print(f"Match minute: {match_minute_tracker[0]}")

if __name__ == "__main__":
    collect(
        video_ids=[
            "C76yNyVe34g",
            "oYUnkHaa-RY",
            "lu-94g6raY8",
            "_nrGYACUQdc",
            "ZPPqAiHgS8o",
            "t1Kdo6LVh6A",
            "Vo67GDNKWI4",
            "Zssj39qzGtQ"
        ],
        match_id  = "MEX_VS_RSA_OPENER"
    )