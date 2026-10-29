
from pathlib import Path
import pandas as pd

# 修改成你自己的 CSV 路径
INPUT = Path(r"D:\HLK-DE-2025\S5\Empirical ResearchTask\Proj\data\DDR Patente_Regionaldaten GESIS_final.csv")
OUTPUT = INPUT.parent / "data_audit"
OUTPUT.mkdir(exist_ok=True)

# GESIS CSV 可能采用分号分隔和 Windows 编码
df = pd.read_csv(
    INPUT,
    sep=";",
    encoding="cp1252",
    dtype=str,
    low_memory=False
)

df.columns = df.columns.str.strip().str.lower()

# 仅用于统计，保留原始文件不变
data = df.replace(
    to_replace=r"^\s*$",
    value=pd.NA,
    regex=True
)

print("Total rows:", len(df))
print("Total columns:", len(df.columns))
print("Columns:", df.columns.tolist())

# 1. 检查所有变量的缺失情况
quality = pd.DataFrame({
    "variable": data.columns,
    "non_missing": data.notna().sum().values,
    "missing": data.isna().sum().values,
    "missing_pct": (
        data.isna().mean().values * 100
    ).round(2),
    "unique_values": [
        data[c].nunique(dropna=True)
        for c in data.columns
    ]
})

quality.to_csv(
    OUTPUT / "variable_quality.csv",
    index=False,
    encoding="utf-8-sig"
)

# 2. 检查完整公开文献编号是否重复
id_col = "veroeffentlichungsnummer"

if id_col in df.columns:
    ids = data[id_col].dropna()
    print("Non-missing document IDs:", len(ids))
    print("Unique document IDs:", ids.nunique())
    print("Duplicate document ID rows:",
          ids.duplicated().sum())

    duplicates = data[
        data[id_col].notna()
        & data[id_col].duplicated(keep=False)
    ]
    duplicates.to_csv(
        OUTPUT / "duplicate_document_ids.csv",
        index=False,
        encoding="utf-8-sig"
    )

# 3. 分析关键字段
important = [
    "anmeldedatum",
    "veroeffentlichungsdatum",
    "erfinder",
    "anmelderinhaber",
    "ipchauptklasse",
    "pdfurl",
    "recherchierbarertexturl"
]

important += [
    c for c in df.columns
    if c.startswith("gemeinde_")
]

important = [
    c for c in important
    if c in df.columns
]

quality[
    quality["variable"].isin(important)
].to_csv(
    OUTPUT / "important_variables.csv",
    index=False,
    encoding="utf-8-sig"
)

# 4. 固定随机种子，抽取 30 行用于初步检查
sample = df.sample(
    n=min(30, len(df)),
    random_state=42
)

sample.to_csv(
    OUTPUT / "pilot_sample_30.csv",
    index=False,
    encoding="utf-8-sig"
)

print("Audit complete:", OUTPUT)
