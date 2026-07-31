from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from jinja2 import BaseLoader, Environment, select_autoescape

from .models import ArchivedMessage, Message


EXPORT_FIELDS = {
    "sequence": "序号",
    "date": "日期",
    "time": "时间",
    "speaker": "发送人",
    "text": "修正后文字",
    "original_text": "OCR 原始文字",
    "confidence": "置信度",
    "visible_time": "微信原始时间",
    "voice_duration": "语音时长（秒）",
    "date_source": "日期来源",
    "screenshot_path": "截图路径",
    "coordinates": "OCR 坐标",
    "edited_at": "修改时间",
    "is_deleted": "已删除",
}

DEFAULT_EXPORT_FIELDS = ("date", "time", "speaker", "text", "screenshot_path")


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ partner_name }} - 微信聊天记录</title>
<style>
body{margin:0;background:#f4f5f6;color:#202124;font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;letter-spacing:0}
header{position:sticky;top:0;padding:14px 20px;background:#fff;border-bottom:1px solid #dfe1e5;z-index:2}
header strong{font-size:16px} header span{margin-left:12px;color:#6b7075;font-size:13px}
main{max-width:860px;margin:0 auto;padding:24px 18px 60px}
.row{display:flex;margin:10px 0}.mine{justify-content:flex-end}.system{justify-content:center}
.bubble{max-width:72%;padding:8px 11px;background:#fff;border:1px solid #e1e3e5;border-radius:5px;white-space:pre-wrap;overflow-wrap:anywhere}
.mine .bubble{background:#95ec69;border-color:#86dc5d}.system .bubble{background:transparent;border:0;color:#858b91;font-size:12px;padding:3px}
.meta{display:block;margin-top:3px;color:#8a9096;font-size:11px}
</style>
</head>
<body>
<header><strong>{{ partner_name }}</strong><span>{{ messages|length }} 条识别记录</span></header>
<main>
{% for message in messages %}
<div class="row {% if message.speaker == '我' %}mine{% elif message.speaker == '系统' %}system{% else %}partner{% endif %}">
  <div class="bubble">{{ message.text }}{% if message.occurred_at and message.speaker != '系统' %}<span class="meta">{{ message.occurred_at|display_time }}{% if message.visible_time %} · 微信显示 {{ message.visible_time }}{% endif %}</span>{% endif %}</div>
</div>
{% endfor %}
</main>
</body>
</html>
"""


def export_archive(
    messages: list[Message], partner_name: str, output_base: Path
) -> tuple[Path, Path, Path]:
    output_base.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_base.with_suffix(".json")
    markdown_path = output_base.with_suffix(".md")
    html_path = output_base.with_suffix(".html")

    json_path.write_text(
        json.dumps(
            [message.to_dict() for message in messages], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    markdown_lines = [f"# 我和{partner_name}的微信聊天记录", ""]
    for message in messages:
        if message.speaker == "系统":
            normalized_time = _display_time(message.occurred_at)
            suffix = f" -> {normalized_time}" if normalized_time else ""
            markdown_lines.append(f"> {message.text}{suffix}")
        else:
            time_value = _display_time(message.occurred_at) or message.visible_time
            time_text = f" · {time_value}" if time_value else ""
            markdown_lines.extend(
                [f"**{message.speaker}{time_text}**", "", message.text, ""]
            )
    markdown_path.write_text("\n".join(markdown_lines), encoding="utf-8")

    environment = Environment(
        loader=BaseLoader(), autoescape=select_autoescape(default=True)
    )
    environment.filters["display_time"] = _display_time
    html_path.write_text(
        environment.from_string(HTML_TEMPLATE).render(
            partner_name=partner_name, messages=messages
        ),
        encoding="utf-8",
    )
    return html_path, markdown_path, json_path


def export_records(
    records: list[ArchivedMessage],
    partner_name: str,
    output_base: Path,
    *,
    formats: set[str],
    fields: list[str],
    archive_root: Path,
) -> dict[str, Path]:
    """Export selected metadata only; screenshot files are never copied or embedded."""
    unknown_formats = formats - {"json", "markdown", "xlsx", "html"}
    unknown_fields = set(fields) - EXPORT_FIELDS.keys()
    if unknown_formats or unknown_fields:
        raise ValueError("包含不支持的导出格式或字段")
    if not formats or not fields:
        raise ValueError("至少选择一种格式和一个字段")
    output_base.parent.mkdir(parents=True, exist_ok=True)
    rows = [_record_values(record, fields, archive_root) for record in records]
    labels = [EXPORT_FIELDS[field] for field in fields]
    outputs: dict[str, Path] = {}

    if "json" in formats:
        path = output_base.with_suffix(".json")
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        outputs["json"] = path
    if "markdown" in formats:
        path = output_base.with_suffix(".md")
        header = "| " + " | ".join(labels) + " |"
        separator = "| " + " | ".join("---" for _ in labels) + " |"
        body = [
            "| " + " | ".join(_markdown_cell(row[field]) for field in fields) + " |"
            for row in rows
        ]
        path.write_text(
            "\n".join([f"# {partner_name} 微信聊天记录", "", header, separator, *body]),
            encoding="utf-8",
        )
        outputs["markdown"] = path
    if "html" in formats:
        path = output_base.with_suffix(".html")
        environment = Environment(
            loader=BaseLoader(), autoescape=select_autoescape(default=True)
        )
        template = environment.from_string(
            """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">
<title>{{ partner }} - 微信聊天记录</title><style>
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,\"PingFang SC\",sans-serif;margin:24px;color:#202124}
table{border-collapse:collapse;width:100%}th,td{border:1px solid #dfe2e5;padding:7px;text-align:left;vertical-align:top;white-space:pre-wrap}th{background:#f4f5f6;position:sticky;top:0}
</style></head><body><h1>{{ partner }}</h1><table><thead><tr>{% for label in labels %}<th>{{ label }}</th>{% endfor %}</tr></thead>
<tbody>{% for row in rows %}<tr>{% for field in fields %}<td>{{ row[field] }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></body></html>"""
        )
        path.write_text(
            template.render(
                partner=partner_name, labels=labels, fields=fields, rows=rows
            ),
            encoding="utf-8",
        )
        outputs["html"] = path
    if "xlsx" in formats:
        from openpyxl import Workbook

        path = output_base.with_suffix(".xlsx")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "聊天记录"
        sheet.append(labels)
        for row in rows:
            sheet.append([row[field] for field in fields])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        workbook.save(path)
        outputs["xlsx"] = path
    return outputs


def _record_values(
    record: ArchivedMessage, fields: list[str], archive_root: Path
) -> dict[str, object]:
    message = record.message
    occurred_time = ""
    if message.occurred_at:
        try:
            occurred_time = datetime.fromisoformat(message.occurred_at).strftime(
                "%H:%M"
            )
        except ValueError:
            occurred_time = message.occurred_at
    screenshot_path = os.path.relpath(
        record.source_path.resolve(), archive_root.resolve()
    )
    values: dict[str, object] = {
        "sequence": message.sequence,
        "date": message.occurred_date or "",
        "time": occurred_time,
        "speaker": message.speaker,
        "text": message.text,
        "original_text": message.original_text or message.text,
        "confidence": round(message.confidence, 4),
        "visible_time": message.visible_time or "",
        "voice_duration": message.voice_duration_seconds or "",
        "date_source": message.date_source,
        "screenshot_path": screenshot_path,
        "coordinates": f"{message.x:.6f},{message.y:.6f},{message.width:.6f},{message.height:.6f}",
        "edited_at": message.edited_at or "",
        "is_deleted": message.is_deleted,
    }
    return {field: values[field] for field in fields}


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _display_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value
