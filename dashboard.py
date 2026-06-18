# ============================================
# dashboard.py
# Streamlit dashboard for live sentiment data.
# Upgraded to an ultra-modern, minimalist, dark-mode design.
# ============================================

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import psycopg2
import os
from dotenv import load_dotenv
import time
from group_schedule import MATCHES_SCHEDULE
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# --- PAGE SETUP & MINIMAL THEME ---
st.set_page_config(
    page_title="World Cup 2026 Sentiment Tracker",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Inject custom CSS for a premium, clean, Helvetica, dark UI look
st.markdown("""
    <style>
    /* Force Helvetica / Sans-serif globally */
    html, body, [class*="css"], .stMarkdown, p, h1, h2, h3, h4 {
        font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif !important;
    }
    /* Dark background tune-up */
    .stApp {
        background-color: #0d1117;
        color: #f0f6fc;
    }
    /* Clean custom cards for metrics */
    div[data-testid="stMetricValue"] {
        font-size: 32px !important;
        font-weight: 300 !important;
        color: #ffffff !important;
    }
    div[data-testid="stMetricLabel"] {
        font-size: 11px !important;
        text-transform: uppercase;
        letter-spacing: 1.5px;
        color: #8b949e !important;
    }
    /* Style the subheader stream bars */
    .stream-header {
        font-size: 14px;
        letter-spacing: 1px;
        text-transform: uppercase;
        color: #8b949e;
        border-bottom: 1px solid #21262d;
        padding-bottom: 8px;
        margin-bottom: 15px;
    }
    </style>
""", unsafe_allow_html=True)


# --- DATABASE LOGIC ---
def get_connection():
    return psycopg2.connect(DATABASE_URL)


def get_matches() -> list:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT match_id FROM messages ORDER BY match_id")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_streams(match_id: str) -> list:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT stream_id, stream_title
        FROM messages
        WHERE match_id = %s
    """, (match_id,))
    rows = cur.fetchall()
    conn.close()
    return rows  # list of (stream_id, stream_title)


def get_match_stats(match_id: str) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN hf_label = 'POSITIVE' THEN 1 ELSE 0 END) as positive,
            SUM(CASE WHEN hf_label = 'NEGATIVE' THEN 1 ELSE 0 END) as negative,
            SUM(CASE WHEN hf_label = 'NEUTRAL'  THEN 1 ELSE 0 END) as neutral
        FROM messages
        WHERE match_id = %s
    """, (match_id,))
    row = cur.fetchone()
    conn.close()
    return {"total": row[0], "positive": row[1], "negative": row[2], "neutral": row[3]}


def get_stream_data(match_id: str, stream_id: str) -> pd.DataFrame:
    conn = get_connection()
    df = pd.read_sql("""
        SELECT match_minute, hf_label, hf_score, text, author
        FROM messages
        WHERE match_id = %s AND stream_id = %s
        ORDER BY id ASC
    """, conn, params=(match_id, stream_id))
    conn.close()
    return df


# --- APPLICATION HEADER ---
st.markdown("<h2 style='font-weight: 200; margin-bottom: 0;'>LIVE | WC MATCH SENTIMENT TRACKER</h2>",
            unsafe_allow_html=True)

matches = get_matches()
if not matches:
    st.warning("No match data yet.")
    st.stop()

# Build mode → matchday lookup from schedule
_mode_to_matchday = {m["mode"]: m["matchday"] for m in MATCHES_SCHEDULE}
_schedule_modes   = set(_mode_to_matchday)

test_ids = [mid for mid in matches if mid not in _schedule_modes]
md1_ids  = [mid for mid in matches if "Matchday 1" in _mode_to_matchday.get(mid, "")]
md2_ids  = [mid for mid in matches if "Matchday 2" in _mode_to_matchday.get(mid, "")]
md3_ids  = [mid for mid in matches if "Matchday 3" in _mode_to_matchday.get(mid, "")]


def render_match(match_id: str):
    # --- GLOBAL KEY PERFORMANCE INDICATORS ---
    stats = get_match_stats(match_id)
    st.markdown("<div class='stream-header'>Key Performance Indicators</div>", unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Msgs", f"{stats['total']:,}")
    col2.metric("🙂 Positive", f"+{stats['positive']:,}")
    col3.metric("🙁 Negative", f"-{stats['negative']:,}")
    col4.metric("😐 Neutral", f"{stats['neutral']:,}")

    st.markdown("<br>", unsafe_allow_html=True)

    # --- INDIVIDUAL PER-STREAM GRIDS ---
    streams = get_streams(match_id)

    for stream_id, stream_title in streams:
        st.markdown(f"<div class='stream-header'>📺 Source: {stream_title}</div>", unsafe_allow_html=True)
        df = get_stream_data(match_id, stream_id)

        if df.empty:
            st.write("No streaming context captured yet.")
            continue

        # Split workspace into Layout Grid: Left (Metrics + Line Curve), Right (Live Feed Table)
        left_layout, right_layout = st.columns([3, 2], gap="large")

        with left_layout:
            # Mini Stream Specific KPIs
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Volume", len(df))
            c2.metric("Positive", (df["hf_label"] == "POSITIVE").sum())
            c3.metric("Negative", (df["hf_label"] == "NEGATIVE").sum())
            c4.metric("Neutral", (df["hf_label"] == "NEUTRAL").sum())

            st.markdown("<br>", unsafe_allow_html=True)

            chart_df = df.groupby("match_minute")["hf_score"].mean().reset_index()
            chart_df.columns = ["Minute", "Avg Sentiment"]

            # color each bar based on sentiment
            chart_df["Color"] = chart_df["Avg Sentiment"].apply(
                lambda x: "#4ade80" if x > 0.05 else "#f87171" if x < -0.05 else "#94a3b8"
            )

            fig = go.Figure()

            fig.add_trace(go.Bar(
                x=chart_df["Minute"],
                y=chart_df["Avg Sentiment"],
                marker_color=chart_df["Color"],
                marker_line_width=0,
                name="Sentiment"
            ))

            # overlay smooth spline line on top
            fig.add_trace(go.Scatter(
                x=chart_df["Minute"],
                y=chart_df["Avg Sentiment"],
                mode="lines",
                line=dict(color="#ffffff", width=2, shape="spline"),
                name="Trend",
                showlegend=False
            ))

            fig.update_layout(
                title=dict(
                    text="SENTIMENT OVER TIME (AVG COMP COMPOUND)",
                    font=dict(family="Helvetica Neue, Arial", size=11, color="#8b949e")
                ),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=40, r=20, t=40, b=40),
                bargap=0.15,
                xaxis=dict(
                    showgrid=False,
                    tickmode="linear",
                    tick0=1,
                    dtick=1,
                    tickfont=dict(color="#8b949e", size=10),
                    title=dict(text="Match Minute", font=dict(color="#8b949e", size=10))
                ),
                yaxis=dict(
                    showgrid=True,
                    gridcolor="#21262d",
                    zeroline=True,
                    zerolinecolor="#30363d",
                    tickfont=dict(color="#8b949e", size=10),
                    autorange=True
                ),
                hovermode="x unified",
                showlegend=False
            )

            st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})

        with right_layout:
            st.markdown(
                "<p style='font-size: 11px; letter-spacing: 1.5px; color: #8b949e; text-transform: uppercase; margin-bottom: 12px;'>Latest Conversation Feed</p>",
                unsafe_allow_html=True)

            # Build out clean, structured table view with direct scores listed
            feed_data = []
            colors = {"POSITIVE": "🟢", "NEGATIVE": "🔴", "NEUTRAL": "⚪"}

            for _, row in df.tail(12).iloc[::-1].iterrows():
                emoji = colors.get(row["hf_label"], "⚪")
                # Format rows nicely
                feed_data.append({
                    "Status": emoji,
                    "Username": row['author'],
                    "Message": row['text'],
                    "Score": f"{row['hf_score']:+.2f}"
                })

            if feed_data:
                feed_df = pd.DataFrame(feed_data)
                # Render a beautiful native Streamlit interactive frame
                st.dataframe(
                    feed_df,
                    column_config={
                        "Status": st.column_config.TextColumn("Status", width="small"),
                        "Username": st.column_config.TextColumn("Username", width="medium"),
                        "Message": st.column_config.TextColumn("Message", width="large"),
                        "Score": st.column_config.TextColumn("Score", width="small"),
                    },
                    hide_index=True,
                    use_container_width=True
                )

        st.markdown("<br><br>", unsafe_allow_html=True)


# --- TABBED MATCH SELECTOR ---
tabs = st.tabs(["🧪 Test Streams", "📅 Matchday 1", "📅 Matchday 2", "📅 Matchday 3"])

with tabs[0]:
    if not test_ids:
        st.info("No data collected yet for this matchday.")
    else:
        match_id = st.selectbox("Select Match", test_ids, label_visibility="collapsed")
        render_match(match_id)

with tabs[1]:
    if not md1_ids:
        st.info("No data collected yet for this matchday.")
    else:
        match_id = st.selectbox("Select Match", md1_ids, label_visibility="collapsed")
        render_match(match_id)

with tabs[2]:
    if not md2_ids:
        st.info("No data collected yet for this matchday.")
    else:
        match_id = st.selectbox("Select Match", md2_ids, label_visibility="collapsed")
        render_match(match_id)

with tabs[3]:
    if not md3_ids:
        st.info("No data collected yet for this matchday.")
    else:
        match_id = st.selectbox("Select Match", md3_ids, label_visibility="collapsed")
        render_match(match_id)

# --- AUTO REFRESH AUTOMATION ---
st.markdown("<hr style='border-color: #21262d;'/>", unsafe_allow_html=True)
bottom_col1, bottom_col2 = st.columns([1, 1])
with bottom_col1:
    st.caption("⚡ Live pipeline connected via Railway PostgreSQL")
with bottom_col2:
    st.markdown("<p style='text-align: right; font-size: 12px; color: #8b949e;'>Auto-refreshes every 10 seconds</p>",
                unsafe_allow_html=True)
time.sleep(10)
st.rerun()