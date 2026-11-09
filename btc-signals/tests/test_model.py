import numpy as np
import pytest

from app.features import FEATURES, FeatureSet
from app.model import LogisticModel, top_drivers


def fs(**vals):
    v = {k: 0.0 for k in FEATURES}
    v.update(vals)
    return FeatureSet(v, 0.6, 1e-4, 0.25, 100, 1, context={"ref_spot": 101, "price_to_beat": 100,
                                                          "seconds_left": 90, "rsi_raw": 60})


def test_prior_only_returns_prior():
    m = LogisticModel.prior_only()
    p, parts = m.predict(fs(logit_prior=np.log(0.7 / 0.3), mom_60=3.0))
    assert p == pytest.approx(0.7)
    assert parts["mom_60"] == 0


def test_fit_recovers_signal_and_roundtrip(tmp_path):
    rng = np.random.default_rng(0)
    n = 20000
    X = rng.normal(0, 1, (n, len(FEATURES)))
    i0, i1 = FEATURES.index("logit_prior"), FEATURES.index("ofi_60")
    z = 1.0 * X[:, i0] + 0.5 * X[:, i1]
    y = (rng.random(n) < 1 / (1 + np.exp(-z))).astype(float)
    m = LogisticModel.fit(X, y, l2=1.0)
    assert m.weights["logit_prior"] == pytest.approx(1.0, abs=0.1)
    assert m.weights["ofi_60"] / m.stds["ofi_60"] == pytest.approx(0.5, abs=0.1)
    assert abs(m.weights["rsi"]) < 0.1
    path = tmp_path / "m.json"
    m.save(path)
    m2 = LogisticModel.load(path)
    assert np.allclose(m.predict_matrix(X[:50]), m2.predict_matrix(X[:50]))
    assert LogisticModel.load(tmp_path / "missing.json").meta["kind"] == "prior_only"


def test_predict_matrix_matches_predict():
    rng = np.random.default_rng(3)
    X = rng.normal(0, 1, (500, len(FEATURES)))
    y = (rng.random(500) < 0.5).astype(float)
    m = LogisticModel.fit(X, y)
    row = dict(zip(FEATURES, X[7]))
    assert m.predict(row)[0] == pytest.approx(m.predict_matrix(X[7:8])[0])


def test_top_drivers_text_and_direction():
    m = LogisticModel.prior_only()
    m.weights["ofi_60"] = -0.5
    f = fs(logit_prior=0.4, ofi_60=1.0)
    p, parts = m.predict(f)
    d = top_drivers(parts, f, "UP")
    assert d[0]["feature"] == "ofi_60" and d[0]["supports"] == "against"
    assert d[1]["supports"] == "for" and "price to beat" in d[1]["text"]
