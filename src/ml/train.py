"""Train and evaluate revert-prediction models (students run this via notebooks/machine_learning.ipynb)."""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, confusion_matrix, f1_score,
                             precision_recall_curve, precision_score, recall_score, roc_auc_score, roc_curve)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from src.config import EXPERIMENTS, ROOT
from src.ml.features import BINARY, FEATURES, LABEL, NUMERIC, TIME, assert_no_leakage, load_labeled

SEED = 42
FIG = ROOT / "reports" / "figures"
COLORS = {"majority baseline": "#52514e", "rule: logged-out": "#eda100", "logistic regression": "#2a78d6",
          "random forest": "#1baf7a"}


@dataclass
class Split:
    X_train: pd.DataFrame
    X_test: pd.DataFrame
    y_train: pd.Series
    y_test: pd.Series
    cutoff: pd.Timestamp


def chronological_split(df: pd.DataFrame, test_frac: float = 0.2) -> Split:
    """Earliest (1 - test_frac) of edits -> train, latest -> test. `df` must be sorted by time."""
    assert df["timestamp_utc"].is_monotonic_increasing, "sort by timestamp first"
    cut = int(len(df) * (1 - test_frac))
    tr, te = df.iloc[:cut], df.iloc[cut:]
    return Split(tr[FEATURES], te[FEATURES], tr[LABEL], te[LABEL], te["timestamp_utc"].iloc[0])


class LoggedOutRule(BaseEstimator, ClassifierMixin):
    """Rule baseline: predict 'reverted' exactly when the editor is logged out (no learning)."""

    def fit(self, X, y):
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, X):
        p = X["is_anonymous"].to_numpy(dtype=float)
        return np.column_stack([1 - p, p])

    def predict(self, X):
        return X["is_anonymous"].to_numpy(dtype=int)


def make_models(seed: int = SEED) -> dict:
    """All models as unfitted scikit-learn estimators/pipelines."""
    lr_prep = ColumnTransformer([
        ("num", StandardScaler(), NUMERIC),
        ("bin", "passthrough", BINARY),
        ("time", OneHotEncoder(handle_unknown="ignore"), TIME),  # hour/weekday as categories
    ])
    rf_prep = ColumnTransformer([("all", "passthrough", FEATURES)])  # trees need no scaling
    return {
        "majority baseline": DummyClassifier(strategy="most_frequent"),
        "rule: logged-out": LoggedOutRule(),
        "logistic regression": Pipeline([("prep", lr_prep), ("clf", LogisticRegression(
            class_weight="balanced", max_iter=2000, random_state=seed))]),
        "random forest": Pipeline([("prep", rf_prep), ("clf", RandomForestClassifier(
            n_estimators=300, min_samples_leaf=20, class_weight="balanced_subsample", n_jobs=-1,
            random_state=seed))]),
    }


def evaluate(model, X: pd.DataFrame, y: pd.Series) -> tuple[dict, np.ndarray]:
    """Metrics for one fitted model + its predicted probabilities for 'reverted'."""
    proba = model.predict_proba(X)[:, 1]
    pred = (proba >= 0.5).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    return {
        "accuracy": accuracy_score(y, pred),
        "precision": precision_score(y, pred, zero_division=0),
        "recall": recall_score(y, pred, zero_division=0),
        "f1": f1_score(y, pred, zero_division=0),
        "roc_auc": roc_auc_score(y, proba) if len(np.unique(proba)) > 1 else 0.5,
        "pr_auc": average_precision_score(y, proba),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }, proba


def run_all(df: pd.DataFrame, seed: int = SEED) -> tuple[pd.DataFrame, dict, dict, Split]:
    """Fit every model on train, evaluate on test. Returns (results table, fitted models, probabilities, split)."""
    assert_no_leakage(FEATURES)
    s = chronological_split(df)
    models, results, probas = make_models(seed), [], {}
    for name, m in models.items():
        m.fit(s.X_train, s.y_train)
        metrics, proba = evaluate(m, s.X_test, s.y_test)
        results.append({"model": name, **metrics})
        probas[name] = proba
    return pd.DataFrame(results), models, probas, s


def plot_curves(s: Split, probas: dict, path=FIG / "ml_roc_pr.png") -> None:
    fig, (a, b) = plt.subplots(1, 2, figsize=(12, 4.5))
    base = s.y_test.mean()
    for name, p in probas.items():
        if name == "majority baseline":
            continue
        fpr, tpr, _ = roc_curve(s.y_test, p)
        a.plot(fpr, tpr, color=COLORS[name], lw=2, label=f"{name} (AUC {roc_auc_score(s.y_test, p):.3f})")
        if len(np.unique(p)) <= 2:
            pred = (p >= 0.5).astype(int)
            b.scatter(recall_score(s.y_test, pred), precision_score(s.y_test, pred), s=70, marker="D",
                      color=COLORS[name], zorder=3, label=f"{name} (single point)")
        else:
            prec, rec, _ = precision_recall_curve(s.y_test, p)
            b.plot(rec, prec, color=COLORS[name], lw=2, label=f"{name} (AP {average_precision_score(s.y_test, p):.3f})")
    a.plot([0, 1], [0, 1], ls=":", color="#888", label="random guess")
    b.axhline(base, ls=":", color="#888", label=f"random guess = positive rate {base:.3f}")
    a.set(xlabel="false positive rate", ylabel="true positive rate (recall)", title="ROC curve (test set)")
    b.set(xlabel="recall", ylabel="precision", title="Precision–recall curve (test set)")
    for ax in (a, b):
        ax.legend(frameon=False, fontsize=8)
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_confusions(results: pd.DataFrame, path=FIG / "ml_confusion.png") -> None:
    fig, axes = plt.subplots(1, len(results), figsize=(4 * len(results), 3.6))
    for ax, (_, r) in zip(axes, results.iterrows()):
        cm = np.array([[r.tn, r.fp], [r.fn, r.tp]])
        ax.imshow(cm, cmap="Blues")
        ax.grid(False)
        for i in range(2):
            for j in range(2):
                ax.text(j, i, f"{cm[i, j]:,}", ha="center", va="center",
                        color="white" if cm[i, j] > cm.max() / 2 else "black")
        ax.set_xticks([0, 1], ["pred: kept", "pred: reverted"])
        ax.set_yticks([0, 1], ["true: kept", "true: reverted"])
        ax.set_title(r.model, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def importance(models: dict, s: Split, seed: int = SEED, n_repeats: int = 5) -> tuple[pd.DataFrame, pd.Series]:
    """Permutation importance (drop in PR-AUC on the test set) for LR and RF + LR coefficients."""
    rows = []
    for name in ("logistic regression", "random forest"):
        pi = permutation_importance(models[name], s.X_test, s.y_test, scoring="average_precision",
                                    n_repeats=n_repeats, random_state=seed, n_jobs=-1)
        for f, m, sd in zip(FEATURES, pi.importances_mean, pi.importances_std):
            rows.append({"model": name, "feature": f, "perm_importance": m, "perm_std": sd})
    imp = pd.DataFrame(rows)
    lr = models["logistic regression"]
    names = lr.named_steps["prep"].get_feature_names_out()
    coefs = pd.Series(lr.named_steps["clf"].coef_[0], index=names)
    return imp, coefs


def plot_importance(imp: pd.DataFrame, path=FIG / "ml_feature_importance.png", top: int = 12) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for ax, name in zip(axes, ("logistic regression", "random forest")):
        d = imp[imp.model == name].sort_values("perm_importance").tail(top)
        ax.barh(d.feature, d.perm_importance, xerr=d.perm_std, color=COLORS[name], capsize=3)
        ax.set_title(f"{name}: permutation importance", fontsize=10)
        ax.set_xlabel("drop in PR-AUC when the feature is shuffled")
        ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labeled_csv")
    a = ap.parse_args()
    df = load_labeled(a.labeled_csv)
    results, models, probas, s = run_all(df)
    print(f"train {len(s.X_train):,} edits (reverted {s.y_train.mean():.2%}) | "
          f"test {len(s.X_test):,} edits from {s.cutoff} (reverted {s.y_test.mean():.2%})")
    print(results.round(4).to_string(index=False))
    out = EXPERIMENTS / "metrics" / "ml_results.csv"
    results.to_csv(out, index=False)
    FIG.mkdir(parents=True, exist_ok=True)
    plot_curves(s, probas)
    plot_confusions(results)
    imp, coefs = importance(models, s)
    imp.to_csv(EXPERIMENTS / "metrics" / "ml_feature_importance.csv", index=False)
    coefs.sort_values().to_csv(EXPERIMENTS / "metrics" / "ml_lr_coefficients.csv", header=["coef"])
    plot_importance(imp)
    print(f"-> {out} and reports/figures/ml_*.png")


if __name__ == "__main__":
    main()
