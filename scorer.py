import os
from dotenv import load_dotenv
load_dotenv()
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from transformers import pipeline

# VADER setup
_vader = SentimentIntensityAnalyzer()

# HuggingFace setup (downloads model first time only ~500MB)
_hf = pipeline(
    "sentiment-analysis",
    model="cardiffnlp/twitter-roberta-base-sentiment",
    device=-1  # 0 = use your GPU (4060), -1 = CPU
)

HF_LABELS = {
    "LABEL_0": "NEGATIVE",
    "LABEL_1": "NEUTRAL",
    "LABEL_2": "POSITIVE"
}

def score(text: str) -> dict:
    # VADER scores
    vader_scores = _vader.polarity_scores(text)

    # HuggingFace score
    hf_result = _hf(text, truncation=True)[0]

    return {
        "vader_compound":  vader_scores["compound"],
        "vader_positive":  vader_scores["pos"],
        "vader_negative":  vader_scores["neg"],
        "vader_neutral":   vader_scores["neu"],
        "hf_label":        HF_LABELS[hf_result["label"]],
        "hf_score":        hf_result["score"]
    }

def score_batch(texts: list) -> list:
    vader_results = [_vader.polarity_scores(t) for t in texts]
    hf_raw = _hf(texts, truncation=True, batch_size=512)

    return [
        {
            "vader_compound": v["compound"],
            "vader_positive": v["pos"],
            "vader_negative": v["neg"],
            "vader_neutral":  v["neu"],
            "hf_label":       HF_LABELS[h["label"]],
            "hf_score":       h["score"]
        }
        for v, h in zip(vader_results, hf_raw)
    ]

