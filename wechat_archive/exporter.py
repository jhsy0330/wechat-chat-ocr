from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from jinja2 import BaseLoader, Environment, select_autoescape

from .models import Message


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
        json.dumps([message.to_dict() for message in messages], ensure_ascii=False, indent=2),
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


def _display_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value
