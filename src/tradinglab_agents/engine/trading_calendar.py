from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from tradinglab_agents.models import Bar


@dataclass(frozen=True)
class ExchangeSession:
    calendar: str
    session_date: date
    open_at: datetime
    close_at: datetime
    is_early_close: bool

    @property
    def duration_minutes(self) -> int:
        return int((self.close_at - self.open_at).total_seconds() // 60)


@lru_cache(maxsize=16)
def _calendar_instance(name: str):
    return xcals.get_calendar(name)


@lru_cache(maxsize=100_000)
def _cached_session(
    name: str,
    local_timezone: str,
    session_date: date,
) -> ExchangeSession:
    calendar = _calendar_instance(name)
    label = session_date.isoformat()
    if not calendar.is_session(label):
        raise ValueError(f"{label} is not a {name} trading session")
    timezone = ZoneInfo(local_timezone)

    def local_naive(value) -> datetime:
        python_value = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
        if python_value.tzinfo is None:
            raise ValueError("exchange calendar timestamp must be timezone-aware")
        return python_value.astimezone(timezone).replace(tzinfo=None)

    open_at = local_naive(calendar.session_open(label))
    close_at = local_naive(calendar.session_close(label))
    regular_minutes = 390 if name == "XNYS" else int(
        (close_at - open_at).total_seconds() // 60
    )
    return ExchangeSession(
        calendar=name,
        session_date=session_date,
        open_at=open_at,
        close_at=close_at,
        is_early_close=(close_at - open_at) < timedelta(minutes=regular_minutes),
    )


class ExchangeTradingCalendar:
    """Project-owned adapter over exchange_calendars.

    The external package supplies exchange rules. This adapter exposes naive
    exchange-local datetimes to match the existing Bar and paper-order schemas,
    centralizes validation, and prevents exchange_calendars objects from leaking
    throughout the codebase.
    """

    def __init__(
        self,
        name: str = "XNYS",
        *,
        local_timezone: str = "America/New_York",
    ) -> None:
        self.name = name.upper()
        self.timezone = ZoneInfo(local_timezone)
        try:
            self._calendar = _calendar_instance(self.name)
        except Exception as exc:
            raise ValueError(f"unsupported exchange calendar: {self.name}") from exc

    def is_session(self, session_date: date | str) -> bool:
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        return bool(self._calendar.is_session(target.isoformat()))

    def session(self, session_date: date | str) -> ExchangeSession:
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        return _cached_session(self.name, str(self.timezone), target)

    def sessions(self, start: date | str, end: date | str) -> tuple[ExchangeSession, ...]:
        start_date = date.fromisoformat(start) if isinstance(start, str) else start
        end_date = date.fromisoformat(end) if isinstance(end, str) else end
        if end_date < start_date:
            raise ValueError("calendar end date cannot precede start date")
        labels = self._calendar.sessions_in_range(
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return tuple(self.session(label.date()) for label in labels)

    def next_session(self, session_date: date | str) -> ExchangeSession:
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        label = self._calendar.date_to_session(target.isoformat(), direction="next")
        if label.date() == target:
            label = self._calendar.next_session(label)
        return self.session(label.date())

    def previous_session(self, session_date: date | str) -> ExchangeSession:
        target = date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        label = self._calendar.date_to_session(target.isoformat(), direction="previous")
        if label.date() == target:
            label = self._calendar.previous_session(label)
        return self.session(label.date())

    def validate_bar(
        self,
        bar: Bar,
        *,
        strict_times: bool = True,
    ) -> ExchangeSession:
        if bar.timestamp.date() != bar.open_at.date():
            raise ValueError(
                f"bar crosses session dates for {bar.symbol}: "
                f"open={bar.open_at.isoformat()}, close={bar.timestamp.isoformat()}"
            )
        session = self.session(bar.timestamp.date())
        if strict_times:
            if bar.open_at != session.open_at:
                raise ValueError(
                    f"{bar.symbol} open does not match {self.name} schedule: "
                    f"actual={bar.open_at.isoformat()}, expected={session.open_at.isoformat()}"
                )
            if bar.timestamp != session.close_at:
                raise ValueError(
                    f"{bar.symbol} close does not match {self.name} schedule: "
                    f"actual={bar.timestamp.isoformat()}, expected={session.close_at.isoformat()}"
                )
        if bar.available_at < session.close_at:
            raise ValueError(
                f"{bar.symbol} daily bar became available before scheduled close: "
                f"actual={bar.available_at.isoformat()}, close={session.close_at.isoformat()}"
            )
        return session

    def summary(self, start: date | str, end: date | str) -> dict[str, object]:
        sessions = self.sessions(start, end)
        return {
            "calendar": self.name,
            "timezone": str(self.timezone),
            "start": sessions[0].session_date.isoformat() if sessions else None,
            "end": sessions[-1].session_date.isoformat() if sessions else None,
            "session_count": len(sessions),
            "early_close_count": sum(item.is_early_close for item in sessions),
            "early_closes": [
                {
                    "date": item.session_date.isoformat(),
                    "close_at": item.close_at.isoformat(),
                }
                for item in sessions
                if item.is_early_close
            ],
        }


__all__ = ["ExchangeSession", "ExchangeTradingCalendar"]
