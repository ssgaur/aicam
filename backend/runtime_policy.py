"""Validated runtime policy helpers for long-lived recorder deployments."""


def parse_int_setting(
    value: str | None,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def capture_is_due(
    last_accepted_ts: float | None,
    candidate_ts: float,
    minimum_interval_seconds: int,
) -> bool:
    if minimum_interval_seconds <= 0 or last_accepted_ts is None:
        return True
    return candidate_ts >= last_accepted_ts + minimum_interval_seconds
