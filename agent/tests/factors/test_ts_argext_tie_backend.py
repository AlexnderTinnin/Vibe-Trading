"""ts_argmax / ts_argmin must give the same answer with and without bottleneck.

On tied extrema bottleneck reports the most recent bar while the pandas
fallback used to report the earliest, so alpha101 values changed with whether
the optional dependency happened to be installed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.factors import base


@pytest.fixture(params=[False, True], ids=["pandas", "bottleneck"])
def backend(request, monkeypatch):
    if request.param and not base.HAS_BOTTLENECK:
        pytest.skip("bottleneck is not installed")
    monkeypatch.setattr(base, "HAS_BOTTLENECK", request.param)


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 3, 3, 2, 2, 1, np.nan, 2, 5, 5.0]})


def test_ts_argmax_ties_use_most_recent_bar(backend):
    out = base.ts_argmax(_frame(), 3)["a"].tolist()
    assert out[2:6] == [2.0, 1.0, 0.0, 1.0]
    assert out[-1] == 2.0
    assert np.isnan(out[6:9]).all()


def test_ts_argmin_ties_use_most_recent_bar(backend):
    out = base.ts_argmin(_frame(), 3)["a"].tolist()
    assert out[2:6] == [0.0, 2.0, 2.0, 2.0]
    assert out[-1] == 0.0
