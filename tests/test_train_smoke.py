"""Smoke test: the training pipeline runs end to end on small RANDOM data (no real results)."""
import json

import numpy as np
import pandas as pd

from src.ml.features import build_features
from src.ml.train import chronological_split, importance, run_all


def synthetic(n=600, seed=0):
    rng = np.random.default_rng(seed)
    anon = rng.random(n) < 0.15
    df = pd.DataFrame({
        "rev_id": np.arange(n), "byte_change": rng.integers(-2000, 2000, n), "old_size": rng.integers(0, 50000, n),
        "edit_summary": rng.choice(["fix", "", "/* Plot */", None], n), "is_anonymous": anon,
        "is_minor": rng.random(n) < 0.2, "is_new_page": rng.random(n) < 0.02,
        "tags": [json.dumps(["mobile edit"] if a else []) for a in anon],
        "timestamp_utc": pd.date_range("2026-10-15", periods=n, freq="37min", tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    df["reverted"] = ((rng.random(n) < np.where(anon, 0.3, 0.05))).astype(int)
    return build_features(df).sort_values("timestamp_utc").reset_index(drop=True)


def test_split_is_chronological():
    s = chronological_split(synthetic())
    assert len(s.X_train) == 480 and len(s.X_test) == 120


def test_pipeline_runs():
    df = synthetic()
    results, models, probas, s = run_all(df)
    assert set(results.model) == {"majority baseline", "rule: logged-out", "logistic regression", "random forest"}
    assert results.loc[results.model == "majority baseline", "recall"].item() == 0  # never predicts 'reverted'
    imp, coefs = importance(models, s, n_repeats=1)
    assert not imp.empty and len(coefs) > 0
