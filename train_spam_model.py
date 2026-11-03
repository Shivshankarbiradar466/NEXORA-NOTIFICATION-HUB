"""Train a small demonstration spam classifier from the included starter dataset.

For academic claims, replace/extend this starter data with a properly licensed,
representative dataset and report held-out precision/recall/F1. This is not a
production-grade spam model by itself.
"""
from pathlib import Path
import csv
import joblib
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "spam_training.csv"
OUT = ROOT / "app" / "ml" / "spam_classifier.joblib"

def main():
    texts, labels = [], []
    with DATA.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            text = (row.get("subject", "") + " " + row.get("body", "")).strip()
            if text and row.get("label") in {"spam", "ham"}:
                texts.append(text)
                labels.append(row["label"])
    if len(set(labels)) < 2 or len(texts) < 10:
        raise SystemExit("Training data must contain at least 10 examples across spam and ham labels.")
    model = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=20000, strip_accents="unicode")),
        ("classifier", LogisticRegression(max_iter=1500, class_weight="balanced")),
    ])
    model.fit(texts, labels)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, OUT)
    print(f"Saved {OUT} using {len(texts)} starter examples.")
    print("Important: training-set fit is not an accuracy estimate. Evaluate on a separate labeled test set before reporting results.")

if __name__ == "__main__":
    main()
