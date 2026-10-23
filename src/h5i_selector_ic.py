"""Read-only H5i extraction and diagnostic PIT IC panel construction."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import inspect
import math
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from selector_ic_contract import (
    FACTOR_CONTRACTS,
    HORIZONS,
    SessionCalendar,
    apply_expected_direction,
    forward_return_for_symbol,
    momentum_signal,
    pb_rev_signal,
    previous_session,
    rank_ic,
    signal_cutoff,
    volatility_signal,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_REQUIRED_COLUMNS = {
    "daily_bars": {"ts", "symbol", "open", "close", "change_pct"},
    "valuation": {"ts", "symbol", "pb"},
}
_BAR_COLUMNS = ("session_date", "symbol", "open", "high", "low", "close",
                "volume", "amount", "change_pct", "turnover")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_SQL_TIMEOUT_SECONDS = 60.0
_SQL_MAX_ROWS = 1_000_000


class H5iInputError(ValueError):
    """Invalid date or source input for diagnostic extraction."""


class H5iSchemaError(RuntimeError):
    """The explicit H5i database does not satisfy the required read contract."""


def _as_day(value: str | date) -> str:
    if isinstance(value, datetime):
        raise H5iInputError("dates must not include a time")
    try:
        return value.isoformat() if isinstance(value, date) else date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        raise H5iInputError("date must be ISO YYYY-MM-DD") from exc


def _default_database_factory(path: str, *, read_only: bool):
    database_path = Path(path)
    if not (database_path.is_file() or database_path.is_dir()):
        raise FileNotFoundError(f"H5i database does not exist: {path}")
    import h5i_db

    return h5i_db.Database(path, read_only=read_only)


def _finite_positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


class H5iReadOnlySource:
    """Explicit H5i connection wrapper that refuses writable open and DuckDB fallback."""

    def __init__(self, path: str, database_factory: Callable | None = None):
        if not isinstance(path, str) or not path.strip():
            raise H5iInputError("an explicit H5i database path is required")
        self.path = path
        self._factory = database_factory or _default_database_factory
        self._db = None
        self._schema: dict[str, list[str]] | None = None

    def __enter__(self) -> "H5iReadOnlySource":
        self._db = self._factory(self.path, read_only=True)
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._db is not None:
            self._db.close()
            self._db = None
        return False

    def _frame(self, query: str) -> pd.DataFrame:
        if self._db is None:
            raise RuntimeError("H5i source must be used inside a context manager")
        sql_method = self._db.sql
        try:
            parameters = inspect.signature(sql_method).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        parameter_names = {parameter.name for parameter in parameters}
        accepts_keywords = any(parameter.kind is inspect.Parameter.VAR_KEYWORD
                               for parameter in parameters)
        options = {}
        if accepts_keywords or "timeout" in parameter_names:
            options["timeout"] = _SQL_TIMEOUT_SECONDS
        if accepts_keywords or "max_rows" in parameter_names:
            options["max_rows"] = _SQL_MAX_ROWS
        result = sql_method(query, **options)
        if hasattr(result, "to_pandas"):
            result = result.to_pandas()
        if not isinstance(result, pd.DataFrame):
            raise H5iSchemaError("H5i query did not return a DataFrame")
        return result

    @staticmethod
    def _column_names(frame: pd.DataFrame) -> list[str]:
        for candidate in ("column_name", "column", "name", "Field"):
            if candidate in frame.columns:
                return [str(value).lower() for value in frame[candidate].dropna().tolist()]
        if frame.shape[1] == 0:
            return []
        return [str(value).lower() for value in frame.iloc[:, 0].dropna().tolist()]

    def schema(self) -> dict[str, list[str]]:
        if self._schema is not None:
            return {table: list(columns) for table, columns in self._schema.items()}
        native_tables = getattr(self._db, "tables", None)
        native_schema = getattr(self._db, "schema", None)
        if callable(native_tables) and callable(native_schema):
            table_values = native_tables()
            if not isinstance(table_values, (list, tuple)):
                raise H5iSchemaError("H5i native tables() did not return a list")
            table_names = {str(value).lower(): str(value) for value in table_values}
            if len(table_names) != len(table_values):
                raise H5iSchemaError("H5i database contains duplicate table names")
            tables = set(table_names)
        else:
            tables_frame = self._frame("SHOW TABLES")
            if tables_frame.empty:
                raise H5iSchemaError("H5i database has no tables")
            table_col = next((c for c in ("name", "table_name", "name_1")
                              if c in tables_frame.columns), tables_frame.columns[0])
            table_names = {str(value).lower(): str(value)
                           for value in tables_frame[table_col].dropna().tolist()}
            tables = set(table_names)
        missing = sorted(set(_REQUIRED_COLUMNS) - tables)
        if missing:
            raise H5iSchemaError(f"missing required H5i tables: {','.join(missing)}")
        schema: dict[str, list[str]] = {}
        for table in sorted(tables):
            if callable(native_tables) and callable(native_schema):
                described = native_schema(table_names[table])
                columns = getattr(described, "names", None)
                if columns is None:
                    try:
                        columns = [field.name for field in described]
                    except (TypeError, AttributeError) as exc:
                        raise H5iSchemaError(
                            f"H5i native schema() did not return a named schema for {table}"
                        ) from exc
                schema[table] = [str(column).lower() for column in columns]
            else:
                described = self._frame(f"DESCRIBE {table}")
                schema[table] = self._column_names(described)
        for table, required in _REQUIRED_COLUMNS.items():
            absent = sorted(required - set(schema.get(table, [])))
            if absent:
                raise H5iSchemaError(f"{table} missing required columns: {','.join(absent)}")
        self._schema = schema
        return {table: list(columns) for table, columns in schema.items()}

    def bars_for_sessions(self, start: str | date, end: str | date) -> pd.DataFrame:
        start_day, end_day = _as_day(start), _as_day(end)
        if start_day > end_day:
            raise H5iInputError("bar range start must be <= end")
        self.schema()
        query = (
            "SELECT CAST(ts AS DATE) AS session_date, symbol, open, high, low, close, "
            "volume, amount, change_pct, turnover FROM daily_bars "
            f"WHERE ts >= TIMESTAMP '{start_day} 00:00:00' "
            f"AND ts < TIMESTAMP '{(date.fromisoformat(end_day) + timedelta(days=1)).isoformat()} 00:00:00' "
            "ORDER BY session_date, symbol"
        )
        frame = self._frame(query)
        if frame.empty:
            return pd.DataFrame(columns=list(_BAR_COLUMNS))
        missing = set(_BAR_COLUMNS) - set(frame.columns)
        if missing:
            raise H5iSchemaError(f"daily bars result missing columns: {','.join(sorted(missing))}")
        frame = frame.loc[:, list(_BAR_COLUMNS)].copy()
        frame["session_date"] = pd.to_datetime(frame["session_date"], errors="coerce").dt.date
        frame["symbol"] = frame["symbol"].astype(str)
        if frame["session_date"].isna().any():
            raise H5iSchemaError("daily bars contain an invalid session date")
        return frame

    def trading_sessions(self, start: str | date, end: str | date) -> list[str]:
        """Return observed H5i daily-bar sessions in an explicit bounded range."""
        start_day, end_day = _as_day(start), _as_day(end)
        if start_day > end_day:
            raise H5iInputError("session range start must be <= end")
        self.schema()
        query = (
            "SELECT DISTINCT CAST(ts AS DATE) AS session_date FROM daily_bars "
            f"WHERE ts >= TIMESTAMP '{start_day} 00:00:00' "
            f"AND ts < TIMESTAMP '{(date.fromisoformat(end_day) + timedelta(days=1)).isoformat()} 00:00:00' "
            "ORDER BY session_date"
        )
        frame = self._frame(query)
        if frame.empty:
            return []
        if "session_date" not in frame.columns:
            raise H5iSchemaError("H5i session query did not retain session_date")
        parsed = pd.to_datetime(frame["session_date"], errors="coerce")
        if parsed.isna().any():
            raise H5iSchemaError("H5i sessions contain an invalid date")
        return sorted({value.date().isoformat() for value in parsed})

    @staticmethod
    def _effective_timestamp(raw: object, cutoff: datetime) -> datetime | None:
        if raw is None or pd.isna(raw):
            return None
        if isinstance(raw, datetime):
            value = raw.replace(tzinfo=SHANGHAI) if raw.tzinfo is None else raw.astimezone(SHANGHAI)
        elif isinstance(raw, date):
            if raw >= cutoff.date():
                return None
            value = datetime.combine(raw, time.max, tzinfo=SHANGHAI)
        else:
            text = str(raw).strip()
            try:
                parsed_date = date.fromisoformat(text[:10])
            except ValueError:
                return None
            if len(text) == 10:
                if parsed_date >= cutoff.date():
                    return None
                value = datetime.combine(parsed_date, time.max, tzinfo=SHANGHAI)
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    return None
                value = (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None
                         else parsed.astimezone(SHANGHAI))
        return value if value <= cutoff else None

    def valuation_asof(self, cutoff: datetime,
                       symbols: Iterable[str] | None = None) -> pd.DataFrame:
        self.schema()
        if cutoff.tzinfo is None:
            raise H5iInputError("valuation cutoff must be timezone-aware")
        local_cutoff = cutoff.astimezone(SHANGHAI)
        cutoff_text = local_cutoff.strftime("%Y-%m-%d %H:%M:%S")
        cutoff_day = local_cutoff.date().isoformat()
        earliest_day = (local_cutoff.date() - timedelta(days=400)).isoformat()
        if symbols is None:
            requested = None
        else:
            raw_symbols = [symbols] if isinstance(symbols, str) else list(symbols)
            if any(not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol)
                   for symbol in raw_symbols):
                raise H5iInputError("valuation symbols contain an invalid SQL identifier")
            requested = sorted(set(raw_symbols))
            if not requested:
                return pd.DataFrame(columns=["ts", "symbol", "pb"])
        symbol_predicate = (
            "" if requested is None else
            " AND symbol IN (" + ",".join(f"'{symbol}'" for symbol in requested) + ")"
        )
        query = (
            "SELECT ts, symbol, pb FROM (SELECT ts, symbol, pb, "
            "ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn "
            "FROM valuation "
            f"WHERE ts <= TIMESTAMP '{cutoff_text}' "
            f"AND ts >= TIMESTAMP '{earliest_day} 00:00:00' "
            f"AND ts <> TIMESTAMP '{cutoff_day} 00:00:00'"
            f"{symbol_predicate}) WHERE rn = 1 ORDER BY symbol"
        )
        frame = self._frame(query)
        if not {"ts", "symbol", "pb"}.issubset(frame.columns):
            raise H5iSchemaError("valuation result must retain ts, symbol, and pb")
        if frame.empty:
            return pd.DataFrame(columns=["ts", "symbol", "pb"])
        selected = []
        for _, row in frame.iterrows():
            symbol = str(row["symbol"])
            effective = self._effective_timestamp(row["ts"], local_cutoff)
            if effective is None or (requested is not None and symbol not in requested):
                continue
            selected.append({"ts": row["ts"], "symbol": symbol, "pb": row["pb"],
                             "_effective_ts": effective})
        if not selected:
            return pd.DataFrame(columns=["ts", "symbol", "pb"])
        out = pd.DataFrame(selected).sort_values(["symbol", "_effective_ts"])
        out = out.drop_duplicates("symbol", keep="last").drop(columns="_effective_ts")
        return out.reset_index(drop=True)


def build_diagnostic_panels(source, calendar: Sequence[str | date],
                            start: str | date, end: str | date) -> dict[str, pd.DataFrame]:
    """Build candidate factor/label/IC panels from a bounded explicit source.

    ``calendar`` is a caller-supplied session sequence; its provenance is recorded by
    the CLI bundle writer, not inferred here. Unsupported factors have explicit IC
    rows and never receive synthetic neutral values.
    """
    normalized_sessions = [_as_day(day) for day in calendar]
    if not normalized_sessions:
        raise H5iInputError("calendar has no sessions")
    try:
        session_calendar = SessionCalendar._from_normalized(normalized_sessions)
    except ValueError as exc:
        raise H5iInputError("calendar sessions must be unique") from exc
    sessions = session_calendar.sessions
    start_day, end_day = _as_day(start), _as_day(end)
    if start_day > end_day:
        raise H5iInputError("signal range start must be <= end")
    try:
        start_index = session_calendar.index_of(start_day)
        end_index = session_calendar.index_of(end_day)
    except ValueError as exc:
        raise H5iInputError("signal range bounds must be calendar sessions") from exc
    signal_days = sessions[start_index:end_index + 1]
    if not signal_days:
        raise H5iInputError("signal range contains no sessions")

    schema = source.schema()
    for table, required in _REQUIRED_COLUMNS.items():
        absent = required - set(schema.get(table, []))
        if absent:
            raise H5iSchemaError(f"{table} missing required columns: {','.join(sorted(absent))}")

    max_horizon = max(HORIZONS)
    fetch_start = sessions[max(0, start_index - 21)]
    fetch_end = sessions[min(len(sessions) - 1, end_index + max_horizon - 1)]
    bars = source.bars_for_sessions(fetch_start, fetch_end)
    required_bar_fields = set(_BAR_COLUMNS)
    if not required_bar_fields.issubset(bars.columns):
        missing = sorted(required_bar_fields - set(bars.columns))
        raise H5iSchemaError(f"daily bars result missing columns: {','.join(missing)}")

    bar_map: dict[tuple[str, str], dict] = {}
    for row in bars.to_dict(orient="records"):
        session = _as_day(row["session_date"])
        symbol = str(row["symbol"])
        key = (session, symbol)
        if key in bar_map:
            raise H5iSchemaError(f"duplicate daily bar for {session}/{symbol}")
        bar_map[key] = row

    factor_columns = ["signal_date", "last_closed_bar_date", "factor", "symbol",
                      "value", "raw_value", "audit_value", "orientation", "status",
                      "reason", "source_ts", "source_age_days"]
    label_columns = ["signal_date", "symbol", "horizon", "return", "status",
                     "reason", "settlement_date", "entry_proxy", "label_version"]
    ic_columns = ["signal_date", "factor", "horizon", "orientation", "expected_direction",
                  "ic", "directed_ic", "raw_measure_ic", "pair_count", "universe_count",
                  "signal_valid_count", "label_valid_count", "status", "reason"]
    coverage_columns = ["signal_date", "factor", "horizon", "universe_count",
                        "signal_valid_count", "label_valid_count", "pair_count",
                        "coverage_ratio", "status", "reason"]
    factor_records: list[dict] = []
    label_records: list[dict] = []
    ic_records: list[dict] = []
    coverage_records: list[dict] = []

    for signal_day in signal_days:
        prior_day = previous_session(signal_day, session_calendar)
        prior_index = session_calendar.index_of(prior_day)
        universe = sorted({
            symbol for (bar_day, symbol), row in bar_map.items()
            if bar_day == prior_day and _finite_positive(row.get("close"))
        })
        cutoff = signal_cutoff(signal_day)
        valuation = source.valuation_asof(cutoff, universe)
        valuation_map = {}
        if not valuation.empty:
            if not {"symbol", "pb", "ts"}.issubset(valuation.columns):
                raise H5iSchemaError("as-of valuation must retain ts, symbol, and pb")
            for row in valuation.to_dict(orient="records"):
                effective_ts = H5iReadOnlySource._effective_timestamp(row["ts"], cutoff)
                if effective_ts is None:
                    continue
                symbol = str(row["symbol"])
                existing = valuation_map.get(symbol)
                if existing is None or effective_ts > existing["_effective_ts"]:
                    valuation_map[symbol] = {**row, "_effective_ts": effective_ts}

        values_by_factor: dict[str, dict[str, dict]] = {
            "vol": {}, "mom_rev": {}, "pb_rev": {},
        }
        history_days = sessions[max(0, prior_index - 20):prior_index + 1]
        for symbol in universe:
            close_history = []
            for hist_day in history_days:
                row = bar_map.get((hist_day, symbol))
                close_history.append(row.get("close") if row is not None else None)
            vol = volatility_signal(close_history)
            mom = momentum_signal(close_history)
            pb_row = valuation_map.get(symbol)
            pb = pb_rev_signal(pb_row.get("pb") if pb_row is not None else None)
            source_ts = pb_row.get("ts") if pb_row is not None else None
            effective_ts = pb_row.get("_effective_ts") if pb_row is not None else None
            source_age_days = ((cutoff - effective_ts).total_seconds() / 86400.0
                               if effective_ts is not None else None)
            row_values = {
                "vol": (vol, vol.raw_value, None,
                        FACTOR_CONTRACTS["vol"].stored_orientation, None, None),
                "mom_rev": (mom, mom.raw_value, mom.audit_value,
                            FACTOR_CONTRACTS["mom_rev"].stored_orientation, None, None),
                "pb_rev": (pb, pb.raw_value, None,
                           FACTOR_CONTRACTS["pb_rev"].stored_orientation,
                           source_ts, source_age_days),
            }
            for factor, (metric, raw_value, audit_value, orientation,
                         source_ts, source_age_days) in row_values.items():
                record = {
                    "signal_date": signal_day,
                    "last_closed_bar_date": prior_day,
                    "factor": factor,
                    "symbol": symbol,
                    "value": metric.value,
                    "raw_value": raw_value,
                    "audit_value": audit_value,
                    "orientation": orientation,
                    "status": metric.status,
                    "reason": metric.reason,
                    "source_ts": source_ts,
                    "source_age_days": source_age_days,
                }
                factor_records.append(record)
                values_by_factor[factor][symbol] = record

        labels_by_horizon: dict[int, dict[str, dict]] = {}
        for horizon in HORIZONS:
            labels_for_symbols = {}
            for symbol in universe:
                label = forward_return_for_symbol(
                    bar_map, symbol, signal_day, horizon, session_calendar
                )
                record = {
                    "signal_date": signal_day,
                    "symbol": symbol,
                    "horizon": horizon,
                    "return": label.value,
                    "status": label.status,
                    "reason": label.reason,
                    "settlement_date": label.settlement_date,
                    "entry_proxy": "daily_open",
                    "label_version": "0925_open_to_close_v1",
                }
                label_records.append(record)
                labels_for_symbols[symbol] = record
            labels_by_horizon[horizon] = labels_for_symbols

        for factor, contract in FACTOR_CONTRACTS.items():
            for horizon in HORIZONS:
                label_records_for_horizon = labels_by_horizon[horizon]
                label_valid_count = sum(
                    row["status"] == "ok" and row["return"] is not None
                    for row in label_records_for_horizon.values()
                )
                if contract.source_status == "unsupported":
                    ic_records.append({
                        "signal_date": signal_day, "factor": factor, "horizon": horizon,
                        "orientation": contract.stored_orientation,
                        "expected_direction": contract.expected_direction,
                        "ic": None, "directed_ic": None, "raw_measure_ic": None,
                        "pair_count": 0, "universe_count": len(universe),
                        "signal_valid_count": 0, "label_valid_count": label_valid_count,
                        "status": "unsupported", "reason": contract.unsupported_reason,
                    })
                    coverage_records.append({
                        "signal_date": signal_day, "factor": factor, "horizon": horizon,
                        "universe_count": len(universe), "signal_valid_count": 0,
                        "label_valid_count": label_valid_count, "pair_count": 0,
                        "coverage_ratio": 0.0, "status": "unsupported",
                        "reason": contract.unsupported_reason,
                    })
                    continue

                signal_records = values_by_factor[factor]
                signal_values = {
                    symbol: record["value"] for symbol, record in signal_records.items()
                    if record["status"] == "ok" and record["value"] is not None
                }
                label_values = {
                    symbol: record["return"] for symbol, record in label_records_for_horizon.items()
                    if record["status"] == "ok" and record["return"] is not None
                }
                result = rank_ic(signal_values, label_values)
                raw_measure_ic = None
                if factor == "vol":
                    raw_values = {
                        symbol: record["raw_value"] for symbol, record in signal_records.items()
                        if record["status"] == "ok" and record["raw_value"] is not None
                    }
                    raw_measure_ic = rank_ic(raw_values, label_values).ic
                status = result.status
                reason = None
                if result.status == "insufficient_pairs" and not label_values:
                    label_statuses = {row["status"] for row in label_records_for_horizon.values()}
                    if "not_mature" in label_statuses:
                        status, reason = "not_mature", "horizon_not_settled"
                directed = apply_expected_direction(factor, result.ic)
                pair_count = result.pair_count
                coverage_ratio = pair_count / len(universe) if universe else 0.0
                ic_records.append({
                    "signal_date": signal_day, "factor": factor, "horizon": horizon,
                    "orientation": contract.stored_orientation,
                    "expected_direction": contract.expected_direction,
                    "ic": result.ic, "directed_ic": directed,
                    "raw_measure_ic": raw_measure_ic, "pair_count": pair_count,
                    "universe_count": len(universe),
                    "signal_valid_count": len(signal_values),
                    "label_valid_count": len(label_values),
                    "status": status, "reason": reason,
                })
                coverage_records.append({
                    "signal_date": signal_day, "factor": factor, "horizon": horizon,
                    "universe_count": len(universe),
                    "signal_valid_count": len(signal_values),
                    "label_valid_count": len(label_values), "pair_count": pair_count,
                    "coverage_ratio": coverage_ratio,
                    "status": status, "reason": reason,
                })

    return {
        "factor_values": pd.DataFrame(factor_records, columns=factor_columns),
        "labels": pd.DataFrame(label_records, columns=label_columns),
        "ic": pd.DataFrame(ic_records, columns=ic_columns),
        "coverage": pd.DataFrame(coverage_records, columns=coverage_columns),
    }
