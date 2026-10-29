"""Audit DDR GESIS patent CSV, including publication IDs and docid links.

Run: python 01_audit_patent_csv_enhanced.py
Install: pip install pandas
No requests/downloads; does not change input CSV.
"""
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, unquote
import re
import pandas as pd

INPUT = Path(r"D:\HLK-DE-2025\S5\Empirical ResearchTask\Proj\data\DDR Patente_Regionaldaten GESIS_final.csv")
OUTPUT = INPUT.parent / "data_audit"
SEP = ";"
ENCODING = "cp1252"  # Change to "utf-8-sig" if your CSV uses UTF-8.
ID_COL = "veroeffentlichungsnummer"
URL_COLS = ("pdfurl", "recherchierbarertexturl")
# Filter by application date before auditing; both endpoints are included.
DATE_COL = "anmeldedatum"  # Use "veroeffentlichungsdatum" for publication date.
DATE_START = "1950-01-01"
DATE_END = "1990-10-02"

# The DEPATISnet docid example DD000000000001A1 has 12 digits.
# Unusual legacy identifiers are flagged for review rather than changed or deleted.
STRICT_ID_PATTERN = re.compile(r"^DD\d{12}[A-Z]\d?$", re.IGNORECASE)
KIND_PATTERN = re.compile(r"^DD\d+([A-Z]\d?)$", re.IGNORECASE)


def cleaned(series):
    return series.astype("string").str.strip().replace("", pd.NA)


def docid_from_url(url):
    if pd.isna(url):
        return None
    try:
        # parse_qs handles normal full URLs and URL query strings
        parsed = urlsplit(str(url))
        values = parse_qs(parsed.query).get("docid", [])
        if not values and str(url).lower().startswith("docid="):
            values = parse_qs(str(url)).get("docid", [])
        return unquote(values[0]).strip().upper() if values else None
    except (TypeError, ValueError):
        return None


def save_csv(frame, filename):
    frame.to_csv(OUTPUT / filename, index=False, encoding="utf-8-sig")


def main():
    if not INPUT.is_file():
        raise FileNotFoundError(f"CSV not found. Change INPUT at top of script: {INPUT}")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(INPUT, sep=SEP, encoding=ENCODING, dtype=str,
                     low_memory=False, keep_default_na=False)
    df.columns = df.columns.str.strip().str.lower()
    if ID_COL not in df.columns:
        raise ValueError(f"Missing required column {ID_COL!r}. Available: {df.columns.tolist()}")
    if df.columns.duplicated().any():
        raise ValueError("Duplicate column names after normalizing headers")

    if DATE_COL not in df.columns:
        raise ValueError(f"Missing date column {DATE_COL!r}")
    # Keep source indices so csv_row still refers to the original input record.
    dates = pd.to_datetime(cleaned(df[DATE_COL]), format="%d.%m.%Y", errors="coerce")
    in_range = dates.between(pd.Timestamp(DATE_START), pd.Timestamp(DATE_END), inclusive="both")
    outside_range = dates.notna() & ~in_range
    invalid_dates = dates.isna()
    save_csv(df.loc[outside_range], "outside_date_range.csv")
    save_csv(df.loc[invalid_dates], "missing_or_invalid_dates.csv")
    date_stats = {
        "input_rows": len(df),
        "in_range_rows": int(in_range.sum()),
        "outside_range_rows": int(outside_range.sum()),
        "missing_or_invalid_date_rows": int(invalid_dates.sum()),
    }
    date_summary = pd.DataFrame(list(date_stats.items()), columns=["metric", "count"])
    date_summary["date_column"] = DATE_COL
    date_summary["start_inclusive"] = DATE_START
    date_summary["end_inclusive"] = DATE_END
    save_csv(date_summary, "date_filter_summary.csv")
    print(f"Date filter: {DATE_COL}, {DATE_START} through {DATE_END} (inclusive)")
    for key, value in date_stats.items():
        print(f"{key}: {value}")
    df = df.loc[in_range].copy()

    data = df.copy()
    for col in data.columns:
        data[col] = cleaned(data[col])
    quality = pd.DataFrame({
        "variable": data.columns,
        "non_missing": data.notna().sum().to_numpy(),
        "missing": data.isna().sum().to_numpy(),
        "missing_pct": (data.isna().mean() * 100).round(2).to_numpy(),
        "unique_values": [data[c].nunique(dropna=True) for c in data.columns],
    })
    save_csv(quality, "variable_quality.csv")
    important = ["anmeldedatum", "veroeffentlichungsdatum", "erfinder",
                 "anmelderinhaber", "ipchauptklasse", *URL_COLS]
    important += [c for c in data if c.startswith("gemeinde_")]
    save_csv(quality[quality.variable.isin(important)], "important_variables.csv")
    save_csv(df.sample(n=min(30, len(df)), random_state=42), "pilot_sample_30.csv")

    report = pd.DataFrame({"csv_row": df.index + 2, "publication_id_raw": df[ID_COL]})
    ids = cleaned(df[ID_COL]).str.upper()
    report["publication_id_normalized"] = ids
    report["id_missing"] = ids.isna()
    report["id_format_invalid"] = (~ids.str.fullmatch(STRICT_ID_PATTERN.pattern, na=False)) & ids.notna()
    report["id_duplicate"] = ids.duplicated(keep=False) & ids.notna()

    # Check only URL fields that exist; report missing columns explicitly.
    for col in URL_COLS:
        present = col in df.columns
        urls = cleaned(df[col]) if present else pd.Series(pd.NA, index=df.index, dtype="string")
        docids = urls.map(docid_from_url).astype("string")
        report[f"{col}_missing"] = urls.isna()
        report[f"{col}_docid"] = docids
        report[f"{col}_docid_missing"] = urls.notna() & docids.isna()
        report[f"{col}_id_mismatch"] = (urls.notna() & docids.notna() & ids.notna()
                                            & docids.ne(ids).fillna(False))
        if not present:
            print(f"WARNING: column {col!r} absent in source CSV")

    issue_cols = ["id_missing", "id_format_invalid", "id_duplicate"]
    for col in URL_COLS:
        issue_cols += [f"{col}_missing", f"{col}_docid_missing", f"{col}_id_mismatch"]
    report["any_issue"] = report[issue_cols].fillna(False).any(axis=1)
    report["issue_types"] = report[issue_cols].fillna(False).apply(
        lambda row: ";".join(name for name in issue_cols if row[name]), axis=1)
    save_csv(report, "id_audit_all_rows.csv")
    save_csv(report.loc[report.any_issue], "id_audit_issues.csv")
    save_csv(df.loc[report.id_duplicate], "duplicate_document_ids.csv")
    save_csv(df.loc[report.any_issue], "id_issues_original_rows.csv")

    kinds = ids.str.extract(KIND_PATTERN.pattern, expand=False).fillna("UNRECOGNIZED_OR_MISSING")
    kind_counts = kinds.value_counts(dropna=False).rename_axis("kind_code").reset_index(name="count")
    save_csv(kind_counts, "id_kind_counts.csv")
    stats = {"total_rows": len(df), "distinct_non_missing_ids": ids.nunique(dropna=True),
             "rows_with_any_issue": int(report.any_issue.sum())}
    stats.update({key: int(report[key].sum()) for key in issue_cols})
    save_csv(pd.DataFrame(list(stats.items()), columns=["metric", "count"]), "id_audit_summary.csv")
    print("\n=== DDR Patent ID Audit ===")
    for key, value in stats.items():
        print(f"{key}: {value}")
    print(f"\nResults saved in: {OUTPUT}")
    print("No network access was used; actual patent existence was NOT verified.")


if __name__ == "__main__":
    main()
