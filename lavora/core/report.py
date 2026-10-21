"""Exportable JSON / HTML reports for scans, recoveries, doctor runs and verifications."""
from __future__ import annotations

import html
import json
import time

from .. import __version__
from ..brand import NAME


def build_report(title: str, sections: dict[str, object]) -> dict:
    return {"product": NAME, "version": __version__, "title": title,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sections": sections}


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


def _render(value: object) -> str:
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        cols = list(value[0].keys())
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
        rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>" for r in value)
        return f"<table><tr>{head}</tr>{rows}</table>"
    if isinstance(value, dict):
        return "<table>" + "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
                                   for k, v in value.items()) + "</table>"
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{html.escape(str(v))}</li>" for v in value) + "</ul>"
    return f"<p>{html.escape(str(value))}</p>"


def to_html(report: dict) -> str:
    body = "".join(f"<h2>{html.escape(k)}</h2>{_render(v)}" for k, v in report["sections"].items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(report['title'])}</title>
<style>body{{font:15px system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1c1b22}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0}}th,td{{border:1px solid #ccc;padding:.3rem .5rem;text-align:left}}
th{{background:#f1eff8}}@media(prefers-color-scheme:dark){{body{{background:#17161c;color:#eee}}th{{background:#2a2833}}td,th{{border-color:#444}}}}</style>
</head><body><h1>{html.escape(report['title'])}</h1><p>{html.escape(report['product'])} {html.escape(report['version'])} &middot;
{html.escape(report['generated'])}</p>{body}</body></html>"""


def save(report: dict, path: str) -> None:
    text = to_html(report) if path.lower().endswith((".html", ".htm")) else to_json(report)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
