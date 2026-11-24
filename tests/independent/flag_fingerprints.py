"""Count the flag injector's fingerprints in a built reference store (interrogate-1 item 1, extent table).

  python tests/independent/flag_fingerprints.py <corpus dir>

Reads derived/ref/*/*.parquet with plain DuckDB SQL, not through otb's measures. Prints JSON: rows per table,
records of the flag services, span events and metrics named feature_flag, rows carrying an attribute key that
contains `feature_flag` (per table and per key), and, as a wider net, rows whose strings mention a catalogued
flag name. `fingerprints_total` sums everything the normaliser must remove.
"""

import json
import sys
from pathlib import Path

import duckdb
import yaml

REPO = Path(__file__).resolve().parents[2]
FLAG_SERVICES = ("flagd", "flagd-ui")
RECORD_TABLES = ("spans", "span_events", "logs", "points")
ATTR_TABLES = ("resources", "spans", "span_events", "logs", "points")
STRING_COLUMNS = {
    "spans": ("name", "status_message"),
    "span_events": ("name",),
    "logs": ("body",),
    "points": ("metric",),
}


def scan(corpus: Path) -> dict:
    ref = corpus / "derived" / "ref"
    con = duckdb.connect()
    con.execute("SET threads = 1")
    for t in ATTR_TABLES:
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{ref / t / '*.parquet'}')")
    q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
    services = ", ".join(f"'{s}'" for s in FLAG_SERVICES)
    flags = sorted(yaml.safe_load((REPO / "scenarios/faults.yaml").read_text())["faults"])
    out: dict = {"rows": {t: q(f"SELECT count(*) FROM {t}")[0][0] for t in ATTR_TABLES}}
    out["flag_service_records"] = {
        t: q(f"SELECT count(*) FROM {t} WHERE service IN ({services})")[0][0] for t in RECORD_TABLES
    }
    out["flag_service_resources"] = q(f"SELECT count(*) FROM resources WHERE service IN ({services})")[0][0]
    out["feature_flag_span_events"] = q("SELECT count(*) FROM span_events WHERE contains(name, 'feature_flag')")[0][0]
    out["feature_flag_metric_points"] = q("SELECT count(*) FROM points WHERE contains(metric, 'feature_flag')")[0][0]
    out["feature_flag_metric_names"] = [
        r[0] for r in q("SELECT DISTINCT metric FROM points WHERE contains(metric, 'feature_flag') ORDER BY 1")
    ]
    has_key = "len(list_filter(map_keys(attrs), k -> contains(k, 'feature_flag'))) > 0"
    out["rows_with_feature_flag_attr"] = {t: q(f"SELECT count(*) FROM {t} WHERE {has_key}")[0][0] for t in ATTR_TABLES}
    out["feature_flag_attr_keys"] = {
        t: dict(
            q(
                f"SELECT k, count(*) FROM (SELECT unnest(map_keys(attrs)) AS k FROM {t}) "
                "WHERE contains(k, 'feature_flag') GROUP BY k ORDER BY k"
            )
        )
        for t in ATTR_TABLES
    }
    mention = " OR ".join(f"contains(s, '{f}')" for f in flags)
    out["flag_name_mentions"] = {}
    for t, cols in STRING_COLUMNS.items():
        strings = " UNION ALL ".join(f"SELECT {c} AS s FROM {t}" for c in cols)
        values = f"SELECT unnest(map_values(attrs)) AS s FROM {t}"
        out["flag_name_mentions"][t] = q(f"SELECT count(*) FROM ({strings} UNION ALL {values}) WHERE {mention}")[0][0]
    out["fingerprints_total"] = (
        sum(out["flag_service_records"].values())
        + out["flag_service_resources"]
        + out["feature_flag_span_events"]
        + out["feature_flag_metric_points"]
        + sum(out["rows_with_feature_flag_attr"].values())
    )
    return out


if __name__ == "__main__":
    print(json.dumps(scan(Path(sys.argv[1])), indent=1, sort_keys=True))
