# ============================================
# database.py
# Handles all SQLite read/write operations.
# Only file in the project that speaks SQL.
#
# Functions:
#   init_db()         - create tables if needed
#   insert_message()  - save one scored message
#   get_match_sentiment() - read data for dashboard
# ============================================

import sqlite3
import os

DB_PATH = "worldcup.db"
GOAL_WORDS = ["goal", "gol", "but", "goall", "goool", "scored", "scores"]
VAR_WORDS  = ["var", "offside", "penalty", "pen ", "referee", "foul"]

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS teams (
            team_id          TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            group_stage      TEXT,
            eliminated       INTEGER DEFAULT 0,
            stage_reached    TEXT DEFAULT NULL,
            total_mentions   INTEGER DEFAULT 0,
            avg_sentiment    REAL DEFAULT NULL,
            matches_played   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS matches (
            match_id         TEXT PRIMARY KEY,
            home_team_id     TEXT,
            away_team_id     TEXT,
            kickoff_time     DATETIME,
            venue            TEXT,
            stage            TEXT,
            home_score       INTEGER DEFAULT NULL,
            away_score       INTEGER DEFAULT NULL,
            status           TEXT DEFAULT 'upcoming',
            total_messages   INTEGER DEFAULT 0,
            avg_sentiment    REAL DEFAULT NULL,
            peak_positive_min INTEGER DEFAULT NULL,
            peak_negative_min INTEGER DEFAULT NULL,
            FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
            FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
        );

        CREATE TABLE IF NOT EXISTS players (
            player_id           TEXT PRIMARY KEY,
            full_name           TEXT NOT NULL,
            team_id             TEXT,
            position            TEXT,
            shirt_number        INTEGER,
            club                TEXT,
            total_mentions      INTEGER DEFAULT 0,
            avg_sentiment       REAL DEFAULT NULL,
            most_positive_score REAL DEFAULT NULL,
            most_negative_score REAL DEFAULT NULL,
            sentiment_trend     TEXT DEFAULT NULL,
            matches_played      INTEGER DEFAULT 0,
            FOREIGN KEY (team_id) REFERENCES teams(team_id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id          TEXT,
            source            TEXT,        -- 'youtube' | 'twitch' | 'reddit'
            timestamp         DATETIME,
            match_minute      INTEGER,
            author            TEXT,
            text              TEXT,
            vader_compound    REAL,
            vader_positive    REAL,
            vader_negative    REAL,
            vader_neutral     REAL,
            hf_label          TEXT,
            hf_score          REAL,
            char_count        INTEGER,
            word_count        INTEGER,
            caps_ratio        REAL,
            exclamation_count INTEGER,
            question_count    INTEGER,
            emoji_count       INTEGER,
            has_goal_word     INTEGER DEFAULT 0,
            has_var_mention   INTEGER DEFAULT 0,
            has_player_name   INTEGER DEFAULT 0,
            mentioned_players TEXT,        -- Comma-separated IDs: "mbappe_fr,griezmann_fr"
            FOREIGN KEY (match_id) REFERENCES matches(match_id)
        );

        -- CRITICAL PERFORMANCE INDEXES FOR ANALYTICS --
        CREATE INDEX IF NOT EXISTS idx_msg_match_minute ON messages(match_id, match_minute);
        CREATE INDEX IF NOT EXISTS idx_msg_source ON messages(source);
        CREATE INDEX IF NOT EXISTS idx_msg_player_flag ON messages(has_player_name) WHERE has_player_name = 1;
        CREATE INDEX IF NOT EXISTS idx_msg_var_flag ON messages(has_var_mention) WHERE has_var_mention = 1;
    """)

    conn.commit()
    conn.close()

def insert_message(match_id: str, source: str, timestamp: str,
                       match_minute: int, author: str, text: str, scores: dict):
        # --- text metrics (calculated here so the collector stays dumb) ---
        words = text.split()
        caps_chars = sum(1 for c in text if c.isupper())
        alpha_chars = sum(1 for c in text if c.isalpha())
        emoji_count = sum(1 for c in text if ord(c) > 127)

        conn = get_connection()
        conn.execute("""
            INSERT INTO messages (
                match_id, source, timestamp, match_minute, author, text,
                vader_compound, vader_positive, vader_negative, vader_neutral,
                hf_label, hf_score,
                char_count, word_count, caps_ratio,
                exclamation_count, question_count, emoji_count,
                has_goal_word, has_var_mention
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?
            )
        """, (
            match_id, source, timestamp, match_minute, author, text,
            scores["vader_compound"], scores["vader_positive"],
            scores["vader_negative"], scores["vader_neutral"],
            scores["hf_label"], scores["hf_score"],
            len(text), len(words),
            round(caps_chars / alpha_chars, 3) if alpha_chars > 0 else 0.0,
            text.count("!"), text.count("?"), emoji_count,
            1 if any(w in text.lower() for w in GOAL_WORDS) else 0,
            1 if any(w in text.lower() for w in VAR_WORDS) else 0
        ))
        conn.commit()
        conn.close()

def get_match_sentiment(match_id: str) -> list:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            match_minute,
            AVG(hf_score) as avg_sentiment,
            COUNT(*) as message_count
        FROM messages
        WHERE match_id = ?
        GROUP BY match_minute
        ORDER BY match_minute ASC
    """, (match_id,))

    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "minute": row[0],
            "avg_sentiment": row[1],
            "message_count": row[2]
        }
        for row in rows
    ]

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully!")
    print(f"Created: {DB_PATH}")