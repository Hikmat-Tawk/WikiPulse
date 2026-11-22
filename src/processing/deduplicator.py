"""De-duplicate normalised rows by `rev_id`."""
from __future__ import annotations

import pandas as pd


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Return (deduplicated frame, number of rows removed). Rows without rev_id are kept as-is."""
    has_id = df["rev_id"].notna()
    with_id = (df[has_id]
               .sort_values(["rev_id", "collection_timestamp_utc"], kind="stable")
               .drop_duplicates(subset="rev_id", keep="first"))
    result = pd.concat([with_id, df[~has_id]]).sort_values("timestamp_utc", kind="stable").reset_index(drop=True)
    return result, len(df) - len(result)
