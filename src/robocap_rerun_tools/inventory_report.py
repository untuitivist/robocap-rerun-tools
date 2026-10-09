"""Small UTF-8 CSV and escaped HTML helpers shared by local inventory tools."""

import csv
import html
from pathlib import Path


def cell_text(value):
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return "" if value is None else str(value)


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: cell_text(row.get(key)) for key in fields})


def render_table(rows: list[dict], columns: dict[str, str]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for label in columns.values())
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(cell_text(row.get(key)))}</td>" for key in columns)
        + "</tr>"
        for row in rows
    )
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
