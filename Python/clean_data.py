"""清理专利 CSV：python clean_data.py（不覆盖原始数据）。"""

import argparse
import csv
import json
from pathlib import Path


DEFAULT_INPUT = Path(__file__).resolve().parent / "DDR Patente_Regionaldaten GESIS_final.csv"


def clean_csv(input_path, output_path, encoding="cp1252"):
    """保留列顺序和所有非空记录，仅清理字段首尾空白。

    缺失值保留为空字符串；专利编号、日期、德文及 URL 保留原格式。
    不自动删除重复记录，避免误删具有研究意义的数据。
    """
    if input_path.resolve() == output_path.resolve():
        raise ValueError("输出文件不能与原始文件相同。")
    report_path = output_path.with_suffix(".report.json")
    if report_path.resolve() == input_path.resolve():
        raise ValueError("报告文件不能与原始文件相同。")
    # 先检查输入，输出使用 x 模式，防止覆盖已有清理结果。
    with input_path.open("r", encoding=encoding, newline="") as source:
        reader = csv.reader(source, delimiter=";")
        original_header = next(reader, None)
        if not original_header:
            raise ValueError("输入 CSV 为空或缺少表头。")
        header = [name.strip() for name in original_header]
        if not all(header) or len(set(header)) != len(header):
            raise ValueError("表头存在空列名或重复列名。")
        if report_path.exists():
            raise FileExistsError(f"报告已存在，请更换输出文件名：{report_path}")
        report = {
            "input": str(input_path.resolve()),
            "output": str(output_path.resolve()),
            "input_encoding": encoding,
            "output_encoding": "utf-8-sig",
            "rows_read": 0,
            "rows_written": 0,
            "blank_rows_skipped": 0,
            "fields_trimmed": 0,
            "missing_by_column": dict.fromkeys(header, 0),
        }
        with output_path.open("x", encoding="utf-8-sig", newline="") as target:
            writer = csv.writer(target, delimiter=";")
            writer.writerow(header)
            for row in reader:
                report["rows_read"] += 1
                cleaned = [value.strip() for value in row]
                if not any(cleaned):
                    report["blank_rows_skipped"] += 1
                    continue
                if len(row) != len(header):
                    raise ValueError(
                        f"CSV 第 {reader.line_num} 行列数异常："
                        f"期望 {len(header)}，实际 {len(row)}。输出可能不完整。"
                    )
                report["fields_trimmed"] += sum(a != b for a, b in zip(row, cleaned))
                for name, value in zip(header, cleaned):
                    if not value:
                        report["missing_by_column"][name] += 1
                writer.writerow(cleaned)
                report["rows_written"] += 1
    with report_path.open("x", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="原始 CSV 路径")
    parser.add_argument("--output", type=Path, help="清理结果路径（必须不存在）")
    parser.add_argument("--encoding", default="cp1252", help="输入编码，默认 cp1252")
    args = parser.parse_args()
    output = args.output or args.input.with_name(args.input.stem + "_cleaned.csv")
    report = clean_csv(args.input, output, args.encoding)
    print(f"清理完成：{report['rows_written']} 行，去除空白 {report['fields_trimmed']} 处。")
    print(f"结果：{output.resolve()}")
    print(f"报告：{output.with_suffix('.report.json').resolve()}")


if __name__ == "__main__":
    main()
