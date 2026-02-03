#!/usr/bin/env python3
import argparse
import calendar
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class TokenEvent:
    timestamp: datetime
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int


def parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def normalize_model_name(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    return raw.strip()


def get_model_pricing_per_million(model: str) -> Optional[tuple[float, float, float]]:
    pricing = {
        # GPT-5.2
        "gpt-5.2": (1.75, 0.175, 14.00),
        "gpt-5.2-chat-latest": (1.75, 0.175, 14.00),
        "gpt-5.2-codex": (1.75, 0.175, 14.00),
        # GPT-5.1
        "gpt-5.1": (1.25, 0.125, 10.00),
        "gpt-5.1-chat-latest": (1.25, 0.125, 10.00),
        "gpt-5.1-codex": (1.25, 0.125, 10.00),
        "gpt-5.1-codex-max": (1.25, 0.125, 10.00),
        "gpt-5.1-codex-mini": (0.25, 0.025, 2.00),
        # GPT-5
        "gpt-5": (1.25, 0.125, 10.00),
        "gpt-5-chat-latest": (1.25, 0.125, 10.00),
        "gpt-5-mini": (0.25, 0.025, 2.00),
        "gpt-5-nano": (0.05, 0.005, 0.40),
        # Codex
        "gpt-5-codex": (1.25, 0.125, 10.00),
    }

    if model in pricing:
        return pricing[model]

    # Map codex variants that include gpt-5.2 to gpt-5.2 pricing.
    if model.startswith("gpt-5.2") and "codex" in model:
        return pricing.get("gpt-5.2")

    return None


def parse_datetime_input(raw: str, local_tz: timezone) -> Optional[datetime]:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def iter_jsonl_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        if Path(dirpath).name == "legacy":
            dirnames[:] = []
            continue
        for name in filenames:
            if name.endswith(".jsonl"):
                yield Path(dirpath) / name


def iter_all_jsonl_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if root.exists():
            yield from iter_jsonl_files(root)


def extract_file_data(path: Path) -> Tuple[List[Interval], List[TokenEvent], int]:
    intervals: List[Interval] = []
    token_events: List[TokenEvent] = []
    pending_start: Optional[datetime] = None
    pending_candidate_end: Optional[datetime] = None
    bad_lines = 0
    prev_total_tokens: Optional[int] = None
    prev_totals: dict[str, int] = {}
    current_model = "unknown"

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue

                ts = parse_timestamp(item.get("timestamp"))
                if ts is None:
                    continue

                item_type = item.get("type")
                if item_type == "turn_context":
                    payload = item.get("payload") or {}
                    model = payload.get("model")
                    current_model = normalize_model_name(model)
                elif item_type == "event_msg":
                    payload = item.get("payload") or {}
                    msg_type = payload.get("type")
                    if msg_type == "user_message":
                        if pending_start is None:
                            pending_start = ts
                            pending_candidate_end = None
                    elif msg_type == "agent_message":
                        if pending_start is not None:
                            if ts >= pending_start:
                                intervals.append(Interval(pending_start, ts))
                            pending_start = None
                            pending_candidate_end = None
                    elif msg_type == "token_count":
                        info = payload.get("info") or {}
                        last_usage = info.get("last_token_usage") or {}
                        total_usage = info.get("total_token_usage") or {}
                        tokens: Optional[int] = None
                        input_tokens: Optional[int] = None
                        cached_input_tokens: Optional[int] = None
                        output_tokens: Optional[int] = None
                        reasoning_tokens: Optional[int] = None

                        if isinstance(last_usage, dict):
                            tokens = parse_int(last_usage.get("total_tokens"))
                            input_tokens = parse_int(last_usage.get("input_tokens"))
                            cached_input_tokens = parse_int(last_usage.get("cached_input_tokens"))
                            output_tokens = parse_int(last_usage.get("output_tokens"))
                            reasoning_tokens = parse_int(last_usage.get("reasoning_output_tokens"))

                        if tokens is None and isinstance(total_usage, dict):
                            total_tokens = parse_int(total_usage.get("total_tokens"))
                            if total_tokens is not None:
                                if prev_total_tokens is None:
                                    tokens = total_tokens
                                else:
                                    tokens = total_tokens - prev_total_tokens
                                prev_total_tokens = total_tokens

                            if input_tokens is None:
                                current = parse_int(total_usage.get("input_tokens"))
                                if current is not None:
                                    prev = prev_totals.get("input_tokens")
                                    input_tokens = current if prev is None else current - prev
                                    prev_totals["input_tokens"] = current

                            if cached_input_tokens is None:
                                current = parse_int(total_usage.get("cached_input_tokens"))
                                if current is not None:
                                    prev = prev_totals.get("cached_input_tokens")
                                    cached_input_tokens = current if prev is None else current - prev
                                    prev_totals["cached_input_tokens"] = current

                            if output_tokens is None:
                                current = parse_int(total_usage.get("output_tokens"))
                                if current is not None:
                                    prev = prev_totals.get("output_tokens")
                                    output_tokens = current if prev is None else current - prev
                                    prev_totals["output_tokens"] = current

                            if reasoning_tokens is None:
                                current = parse_int(total_usage.get("reasoning_output_tokens"))
                                if current is not None:
                                    prev = prev_totals.get("reasoning_output_tokens")
                                    reasoning_tokens = current if prev is None else current - prev
                                    prev_totals["reasoning_output_tokens"] = current

                        if isinstance(total_usage, dict):
                            total_tokens = parse_int(total_usage.get("total_tokens"))
                            if total_tokens is not None:
                                prev_total_tokens = total_tokens

                        if tokens is None:
                            tokens = 0
                        if input_tokens is None:
                            input_tokens = 0
                        if cached_input_tokens is None:
                            cached_input_tokens = 0
                        if output_tokens is None:
                            output_tokens = 0
                        if reasoning_tokens is None:
                            reasoning_tokens = 0

                        if tokens < 0:
                            tokens = 0
                        if input_tokens < 0:
                            input_tokens = 0
                        if cached_input_tokens < 0:
                            cached_input_tokens = 0
                        if output_tokens < 0:
                            output_tokens = 0
                        if reasoning_tokens < 0:
                            reasoning_tokens = 0

                        total_tokens = tokens
                        if total_tokens == 0:
                            total_tokens = input_tokens + output_tokens + reasoning_tokens

                        token_events.append(
                            TokenEvent(
                                ts,
                                current_model,
                                input_tokens,
                                cached_input_tokens,
                                output_tokens,
                                reasoning_tokens,
                                total_tokens,
                            )
                        )
                elif item_type == "response_item":
                    payload = item.get("payload") or {}
                    if (
                        payload.get("type") == "message"
                        and payload.get("role") == "assistant"
                        and pending_start is not None
                        and pending_candidate_end is None
                    ):
                        pending_candidate_end = ts
    except OSError:
        return intervals, token_events, bad_lines + 1

    if pending_start is not None and pending_candidate_end is not None:
        if pending_candidate_end >= pending_start:
            intervals.append(Interval(pending_start, pending_candidate_end))

    return intervals, token_events, bad_lines


def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x.start)
    merged: List[Interval] = []
    current = intervals[0]
    for interval in intervals[1:]:
        if interval.start <= current.end:
            current = Interval(current.start, max(current.end, interval.end))
        else:
            merged.append(current)
            current = interval
    merged.append(current)
    return merged


def clamp_interval(interval: Interval, window_start: datetime, window_end: datetime) -> Optional[Interval]:
    if interval.end <= window_start or interval.start >= window_end:
        return None
    start = max(interval.start, window_start)
    end = min(interval.end, window_end)
    if end <= start:
        return None
    return Interval(start, end)


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def shift_months(dt: datetime, months: int) -> datetime:
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def resolve_window(
    window: str,
    now: datetime,
    earliest: Optional[datetime],
    start_override: Optional[datetime],
    end_override: Optional[datetime],
) -> Tuple[datetime, datetime, str]:
    if start_override or end_override:
        start = start_override or earliest or now
        end = end_override or now
        label = "custom"
        return start, end, label

    if window == "all":
        start = earliest or now
        return start, now, "all"

    if window == "1d":
        return now - timedelta(days=1), now, "1d"
    if window == "1w":
        return now - timedelta(days=7), now, "1w"
    if window == "1m":
        return shift_months(now, -1), now, "1m"
    if window == "3m":
        return shift_months(now, -3), now, "3m"
    if window == "1y":
        return shift_months(now, -12), now, "1y"

    return now, now, "unknown"


def select_granularity(window_seconds: float) -> str:
    if window_seconds <= 2 * 24 * 3600:
        return "hour"
    if window_seconds <= 120 * 24 * 3600:
        return "day"
    if window_seconds <= 400 * 24 * 3600:
        return "week"
    return "month"


def floor_to_granularity(dt_local: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return dt_local.replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        return dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "week":
        base = dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
        return base - timedelta(days=base.weekday())
    if granularity == "month":
        return dt_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt_local


def add_granularity(dt_local: datetime, granularity: str) -> datetime:
    if granularity == "hour":
        return dt_local + timedelta(hours=1)
    if granularity == "day":
        return dt_local + timedelta(days=1)
    if granularity == "week":
        return dt_local + timedelta(days=7)
    if granularity == "month":
        return shift_months(dt_local, 1)
    return dt_local


def bucketize_intervals(
    intervals: List[Interval],
    window_start: datetime,
    window_end: datetime,
    granularity: str,
    local_tz: timezone,
) -> List[dict]:
    start_local = window_start.astimezone(local_tz)
    end_local = window_end.astimezone(local_tz)
    bucket_start_local = floor_to_granularity(start_local, granularity)

    buckets: List[dict] = []
    idx = 0
    while bucket_start_local < end_local:
        bucket_end_local = add_granularity(bucket_start_local, granularity)
        bucket_start_utc = bucket_start_local.astimezone(timezone.utc)
        bucket_end_utc = bucket_end_local.astimezone(timezone.utc)

        bucket_start_utc = max(bucket_start_utc, window_start)
        bucket_end_utc = min(bucket_end_utc, window_end)
        if bucket_end_utc <= bucket_start_utc:
            bucket_start_local = bucket_end_local
            continue

        total = 0.0
        while idx < len(intervals) and intervals[idx].end <= bucket_start_utc:
            idx += 1

        scan = idx
        while scan < len(intervals) and intervals[scan].start < bucket_end_utc:
            overlap_start = max(intervals[scan].start, bucket_start_utc)
            overlap_end = min(intervals[scan].end, bucket_end_utc)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
            if intervals[scan].end <= bucket_end_utc:
                scan += 1
            else:
                break
        if scan > idx and (scan == len(intervals) or intervals[scan].end <= bucket_end_utc):
            idx = scan

        bucket_seconds = (bucket_end_utc - bucket_start_utc).total_seconds()
        percent = (total / bucket_seconds) * 100 if bucket_seconds > 0 else 0.0

        buckets.append(
            {
                "bucket_start": bucket_start_local.isoformat(),
                "bucket_end": bucket_end_local.isoformat(),
                "active_seconds_any_instance": total,
                "percent_any_instance": percent,
            }
        )

        bucket_start_local = bucket_end_local

    return buckets


def bucketize_tokens(
    token_events: List[TokenEvent],
    window_start: datetime,
    window_end: datetime,
    granularity: str,
    local_tz: timezone,
) -> List[dict]:
    start_local = window_start.astimezone(local_tz)
    end_local = window_end.astimezone(local_tz)
    bucket_start_local = floor_to_granularity(start_local, granularity)

    events = sorted(token_events, key=lambda event: event.timestamp)
    idx = 0
    buckets: List[dict] = []

    while bucket_start_local < end_local:
        bucket_end_local = add_granularity(bucket_start_local, granularity)
        bucket_start_utc = max(bucket_start_local.astimezone(timezone.utc), window_start)
        bucket_end_utc = min(bucket_end_local.astimezone(timezone.utc), window_end)

        if bucket_end_utc <= bucket_start_utc:
            bucket_start_local = bucket_end_local
            continue

        while idx < len(events) and events[idx].timestamp < bucket_start_utc:
            idx += 1

        total_tokens = 0
        scan = idx
        while scan < len(events) and events[scan].timestamp < bucket_end_utc:
            total_tokens += events[scan].total_tokens
            scan += 1
        idx = scan

        buckets.append(
            {
                "bucket_start": bucket_start_local.isoformat(),
                "bucket_end": bucket_end_local.isoformat(),
                "tokens": total_tokens,
            }
        )
        bucket_start_local = bucket_end_local

    return buckets


def compute_uptime(
    root: Optional[Path] = None,
    roots: Optional[List[Path]] = None,
    window: str = "all",
    start: str = "",
    end: str = "",
    granularity: str = "",
) -> dict:
    if roots is None:
        if root is None:
            raise FileNotFoundError("No sessions directory provided.")
        roots = [root]

    roots = [Path(os.path.expanduser(str(item))) for item in roots]
    if not any(item.exists() for item in roots):
        raise FileNotFoundError("No sessions directory found.")

    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    start_override = parse_datetime_input(start, local_tz) if start else None
    end_override = parse_datetime_input(end, local_tz) if end else None

    intervals: List[Interval] = []
    token_events: List[TokenEvent] = []
    files_scanned = 0
    bad_lines = 0
    earliest: Optional[datetime] = None

    for path in iter_all_jsonl_files(roots):
        files_scanned += 1
        file_intervals, file_tokens, file_bad = extract_file_data(path)
        bad_lines += file_bad
        if file_intervals:
            file_earliest = min(interval.start for interval in file_intervals)
            if earliest is None or file_earliest < earliest:
                earliest = file_earliest
            intervals.extend(file_intervals)
        if file_tokens:
            token_events.extend(file_tokens)
            file_token_earliest = min(event.timestamp for event in file_tokens)
            if earliest is None or file_token_earliest < earliest:
                earliest = file_token_earliest

    now = datetime.now(timezone.utc)
    window_start, window_end, window_label = resolve_window(
        window, now, earliest, start_override, end_override
    )

    if window_end <= window_start:
        raise ValueError("Invalid window: end must be after start.")

    clamped: List[Interval] = []
    for interval in intervals:
        adjusted = clamp_interval(interval, window_start, window_end)
        if adjusted:
            clamped.append(adjusted)

    merged = merge_intervals(clamped)
    raw_seconds = sum((interval.end - interval.start).total_seconds() for interval in clamped)
    merged_seconds = sum((interval.end - interval.start).total_seconds() for interval in merged)
    window_seconds = (window_end - window_start).total_seconds()

    chosen_granularity = granularity or select_granularity(window_seconds)
    token_buckets = bucketize_tokens(
        token_events,
        window_start=window_start,
        window_end=window_end,
        granularity=chosen_granularity,
        local_tz=local_tz,
    )
    tokens_total = sum(bucket["tokens"] for bucket in token_buckets)

    cost_total = 0.0
    unknown_models: set[str] = set()
    for event in token_events:
        if event.timestamp < window_start or event.timestamp > window_end:
            continue
        pricing = get_model_pricing_per_million(event.model)
        if pricing is None:
            unknown_models.add(event.model)
            continue
        input_rate, cached_rate, output_rate = pricing
        cached = min(event.cached_input_tokens, event.input_tokens)
        uncached = max(0, event.input_tokens - cached)
        output = event.output_tokens + event.reasoning_tokens
        cost_total += (uncached / 1_000_000) * input_rate
        cost_total += (cached / 1_000_000) * cached_rate
        cost_total += (output / 1_000_000) * output_rate

    cost_partial = len(unknown_models) > 0

    any_pct = (merged_seconds / window_seconds) * 100 if window_seconds > 0 else 0.0
    raw_pct = (raw_seconds / window_seconds) * 100 if window_seconds > 0 else 0.0

    return {
        "window": window_label,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "granularity": chosen_granularity,
        "token_buckets": token_buckets,
        "tokens_total": tokens_total,
        "cost_total_usd": cost_total,
        "cost_partial": cost_partial,
        "unknown_models": sorted(unknown_models),
        "files_scanned": files_scanned,
        "intervals_raw": len(clamped),
        "intervals_merged": len(merged),
        "active_seconds_any_instance": merged_seconds,
        "active_seconds_summed": raw_seconds,
        "percent_any_instance": any_pct,
        "percent_summed": raw_pct,
        "bad_lines": bad_lines,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Estimate Codex active-time usage from ~/.codex/sessions JSONL logs."
    )
    parser.add_argument(
        "--root",
        default=str(Path.home() / ".codex" / "sessions"),
        help="Path to the Codex sessions directory.",
    )
    parser.add_argument(
        "--window",
        choices=["all", "1d", "1w", "1m", "3m", "1y"],
        default="all",
        help="Time window for the uptime percentage.",
    )
    parser.add_argument(
        "--granularity",
        choices=["hour", "day", "week", "month"],
        default="",
        help="Bucket size for timeseries (default: auto).",
    )
    parser.add_argument(
        "--start",
        default="",
        help="Override start time (ISO 8601, e.g. 2026-01-15T12:00:00Z).",
    )
    parser.add_argument(
        "--end",
        default="",
        help="Override end time (ISO 8601, e.g. 2026-02-01T12:00:00Z).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of text.",
    )
    args = parser.parse_args()

    root = Path(os.path.expanduser(args.root))
    try:
        result = compute_uptime(
            root=root,
            window=args.window,
            start=args.start,
            end=args.end,
            granularity=args.granularity,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc))
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print(f"Window: {result['window']}")
    print(f"Start:  {result['window_start']}")
    print(f"End:    {result['window_end']}")
    print(f"Files:  {result['files_scanned']}")
    print(f"Raw intervals:    {result['intervals_raw']}")
    print(f"Merged intervals: {result['intervals_merged']}")
    print(
        "Active (any):     "
        f"{format_duration(result['active_seconds_any_instance'])} "
        f"({result['percent_any_instance']:.3f}%)"
    )
    print(
        "Active (summed):  "
        f"{format_duration(result['active_seconds_summed'])} "
        f"({result['percent_summed']:.3f}%)"
    )
    if result["bad_lines"]:
        print(f"Bad lines: {result['bad_lines']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
