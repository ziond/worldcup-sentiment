# ============================================
# youtube_collector.py
# Connects to a YouTube live stream chat.
# Polls for new messages every 5 seconds.
# Scores each message and saves to database.
# ============================================

import time
from datetime import datetime
from dotenv import load_dotenv
import os
from googleapiclient.discovery import build

from scorer import score_batch
from database import insert_message

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")

def get_live_chat_id(youtube, video_id: str) -> str:
    response = youtube.videos().list(
        part="liveStreamingDetails",
        id=video_id
    ).execute()

    return response["items"][0]["liveStreamingDetails"]["activeLiveChatId"]

def collect(video_id: str, match_id: str, match_minute: int):
    youtube = build("youtube", "v3", developerKey=API_KEY)
    live_chat_id = get_live_chat_id(youtube, video_id)

    print(f"Connected to live chat: {live_chat_id}")

    next_page_token = None

    while True:
        response = youtube.liveChatMessages().list(
            liveChatId=live_chat_id,
            part="snippet,authorDetails",
            pageToken=next_page_token
        ).execute()

        messages = response.get("items", [])

        if messages:
            texts = [m["snippet"]["displayMessage"] for m in messages]
            scores = score_batch(texts)

            for m, score in zip(messages, scores):
                insert_message(
                    match_id    = match_id,
                    source      = "youtube",
                    timestamp   = datetime.utcnow().isoformat(),
                    match_minute= match_minute,
                    author      = m["authorDetails"]["displayName"],
                    text        = m["snippet"]["displayMessage"],
                    scores      = score
                )

        next_page_token = response.get("nextPageToken")
        wait = response.get("pollingIntervalMillis", 5000) / 1000
        print(f"Fetched {len(messages)} messages — waiting {wait}s")
        time.sleep(wait)

if __name__ == "__main__":
    collect(
        video_id     = "VmCHJMxtEqE",
        match_id     = "TEST_MATCH_001",
        match_minute = 1
    )