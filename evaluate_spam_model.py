"""Evaluate the starter spam dataset with stratified cross-validation.

These metrics describe only the small included development dataset. For a
credible final-year evaluation, use a larger licensed dataset and a held-out
set that was not used during model development.
"""
from pathlib import Path
import csv
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "spam_training.csv"

def main():
    texts, labels = [], []
    with DATA.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            text = (row.get("subject", "") + " " + row.get("body", "")).strip()
            if text and row.get("label") in {"spam", "ham"}:
                texts.append(text)
                labels.append(row["label"])
    model = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=20000, strip_accents="unicode")),
        ("classifier", LogisticRegression(max_iter=1500, class_weight="balanced")),
    ])
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    predicted = cross_val_predict(model, texts, labels, cv=folds)
    print(f"Development examples: {len(texts)}")
    print("WARNING: these are cross-validation results on the included small starter dataset, not production accuracy.")
    print(classification_report(labels, predicted, labels=["ham", "spam"], zero_division=0))
    print("Confusion matrix (rows=true ham,spam; columns=predicted ham,spam):")
    print(confusion_matrix(labels, predicted, labels=["ham", "spam"]))

if __name__ == "__main__":
    main()
