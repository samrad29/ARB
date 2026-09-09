from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from prediction_arb.money import format_cents

DURATION_BUCKETS = [
    ("1 second", 1),
    ("5 seconds", 5),
    ("10 seconds", 10),
    ("30 seconds", 30),
    ("1 minute", 60),
    ("5 minutes", 300),
    ("1 hour", 3600),
]


def opportunity_history_report(rows: list[dict]) -> str:
    if not rows:
        return "No opportunities recorded yet. Run `python -m prediction_arb.cli scan` first."

    frame = pd.DataFrame(rows)
    now = datetime.now(timezone.utc)
    durations: list[float] = []
    for _, row in frame.iterrows():
        duration = row.get("duration_seconds")
        if duration is None or (isinstance(duration, float) and pd.isna(duration)):
            try:
                start = datetime.fromisoformat(str(row["detected_at"]))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                end_raw = row.get("expired_at")
                end = datetime.fromisoformat(str(end_raw)) if end_raw else now
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                duration = (end - start).total_seconds()
            except (TypeError, ValueError):
                duration = None
        if duration is not None:
            durations.append(float(duration))

    duration_series = pd.Series(durations, dtype="float64")
    lines = [
        "========================================================",
        "OPPORTUNITY HISTORY",
        "========================================================",
        "",
        f"How many opportunities have we seen?     {len(frame):,}",
        f"Currently open:                          {(frame['status'] == 'OPEN').sum():,}",
        f"Expired:                                 {(frame['status'] == 'EXPIRED').sum():,}",
        "",
    ]
    if not duration_series.empty:
        lines.extend(
            [
                f"Median lifetime:                         {_fmt_seconds(duration_series.median())}",
                f"Average lifetime:                        {_fmt_seconds(duration_series.mean())}",
                f"25th percentile:                         {_fmt_seconds(duration_series.quantile(0.25))}",
                f"75th percentile:                         {_fmt_seconds(duration_series.quantile(0.75))}",
                f"Longest opportunity:                     {_fmt_seconds(duration_series.max())}",
                "",
            ]
        )
        lines.append("Survived longer than:")
        for label, seconds in DURATION_BUCKETS:
            count = int((duration_series >= seconds).sum())
            lines.append(f"  {label:<12} {count:,}")
        lines.append("")

    lines.extend(
        [
            f"Average gross edge:                      {format_cents(int(frame['gross_profit'].mean()))}",
            f"Average net edge:                        {format_cents(int(frame['net_profit'].mean()))}",
            f"Maximum executable capital:              {format_cents(int(frame['capital_required'].max()))}",
            f"Average executable capital:              {format_cents(int(frame['capital_required'].mean()))}",
            "",
            "By exchange pair:",
        ]
    )
    pairs = (
        frame.assign(pair=frame["market_a_exchange"] + " / " + frame["market_b_exchange"])
        .groupby("pair")
        .size()
        .sort_values(ascending=False)
    )
    for pair, count in pairs.items():
        lines.append(f"  {pair}: {int(count):,}")
    lines.append("")
    lines.append("By category:")
    categories = frame["market_a_category"].fillna("unknown").replace("", "unknown")
    for category, count in categories.value_counts().items():
        lines.append(f"  {category}: {int(count):,}")
    lines.append("")
    return "\n".join(lines)


def _fmt_seconds(value: float) -> str:
    if value < 1:
        return f"{value * 1000:.0f} ms"
    if value < 60:
        return f"{value:.2f} s"
    minutes, seconds = divmod(value, 60)
    if minutes < 60:
        return f"{int(minutes)}m {seconds:.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"
