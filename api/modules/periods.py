"""Re-export of the shared bucket definitions.

The real module lives at `shared/periods.py` because `process` needs it too: the
daily run re-renders a date's week and month, and a bucket has to mean the same
thing on both sides of the fence.

`front/app/utils/periods.ts` mirrors this arithmetic — change one and change
the other.
"""

from shared.periods import (  # noqa: F401
    PERIODS,
    Period,
    bucket_sql,
    span,
    start_of,
)

__all__ = ["PERIODS", "Period", "bucket_sql", "span", "start_of"]
