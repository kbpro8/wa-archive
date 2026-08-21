"""Write a chat out as html / csv / txt / vcf.

Each exporter returns `(text, mimetype)`. The mimetype is what the reader's
export links serve; the CLI just writes the text to a file.

PDF is deliberately absent. Browser print-to-PDF from the HTML export gives
better typography, pagination and font handling than any Python PDF library,
at zero dependency cost — so the HTML carries a print stylesheet instead.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import sqlite3

FORMATS = ("html", "csv", "txt", "vcf")

# One row per message, with its first attachment folded in.
_ROWS = """
SELECT m.id, m.ts_utc, m.direction, m.type, m.body, m.status, m.event,
       m.is_deleted, m.is_starred,
       COALESCE(ct.display_name, ct.jid) AS sender,
       ct.jid  AS sender_jid,
       md.kind AS media_kind, md.path AS media_path, md.caption AS caption,
       md.bytes AS media_bytes, md.missing AS media_missing,
       md.latitude AS latitude, md.longitude AS longitude, md.vcard AS vcard
FROM messages m
LEFT JOIN contacts ct ON ct.id = m.sender_id
LEFT JOIN media    md ON md.id = (SELECT MIN(id) FROM media WHERE message_id = m.id)
WHERE m.chat_id = ?
ORDER BY m.ts_utc, m.id
"""


def _chat(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT id, jid, name, kind FROM chats WHERE id = ?",
                       (chat_id,)).fetchone()
    if row is None:
        raise LookupError(f"No chat with id {chat_id}")
    return row


def _fetch(conn: sqlite3.Connection, chat_id: int) -> list[sqlite3.Row]:
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(_ROWS, (chat_id,)).fetchall()
    finally:
        conn.row_factory = old


def _who(r: sqlite3.Row) -> str:
    if r["direction"] == "out":
        return "Me"
    return r["sender"] or r["sender_jid"] or "Unknown"


def _local(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts)


def _placeholder(r: sqlite3.Row) -> str:
    """Human-readable stand-in for a message whose payload isn't text."""
    if r["event"]:
        return f"<{r['event'].replace('_', ' ')}>"
    if r["is_deleted"]:
        return "<message deleted>"
    if r["media_kind"] == "location" and r["latitude"] is not None:
        return f"<location {r['latitude']:.5f},{r['longitude']:.5f}>"
    if r["media_kind"]:
        state = " missing from backup" if r["media_missing"] else ""
        label = r["caption"] or r["media_kind"]
        return f"<{r['media_kind']}: {label}{state}>"
    return ""


# ---------------------------------------------------------------------------
# txt
# ---------------------------------------------------------------------------

def _txt(conn, chat_id, media_base):
    chat = _chat(conn, chat_id)
    out = [f"{chat['name'] or chat['jid']}  ({chat['kind']})", "=" * 60, ""]
    for r in _fetch(conn, chat_id):
        stamp = _local(r["ts_utc"]).strftime("%Y-%m-%d %H:%M")
        text = r["body"] or _placeholder(r)
        if r["body"] and (extra := _placeholder(r)):
            text = f"{extra} {r['body']}"
        # Continuation lines are indented so a multi-line message stays one
        # visual block when grepping or reading in a pager.
        body = text.replace("\n", "\n" + " " * 4)
        out.append(f"[{stamp}] {_who(r)}: {body}")
    return "\n".join(out) + "\n", "text/plain; charset=utf-8"


# ---------------------------------------------------------------------------
# csv
# ---------------------------------------------------------------------------

_CSV_COLUMNS = ("id", "datetime_local", "ts_utc", "direction", "sender", "sender_jid",
                "type", "body", "status", "event", "starred", "media_kind",
                "media_file", "media_bytes", "media_missing", "caption",
                "latitude", "longitude")


def _csv(conn, chat_id, media_base):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_CSV_COLUMNS)
    for r in _fetch(conn, chat_id):
        w.writerow([
            r["id"], _local(r["ts_utc"]).strftime("%Y-%m-%d %H:%M:%S"), r["ts_utc"],
            r["direction"], _who(r), r["sender_jid"] or "", r["type"], r["body"] or "",
            r["status"] or "", r["event"] or "", r["is_starred"], r["media_kind"] or "",
            r["media_path"] or "", r["media_bytes"] or "", r["media_missing"] or 0,
            r["caption"] or "", r["latitude"] if r["latitude"] is not None else "",
            r["longitude"] if r["longitude"] is not None else "",
        ])
    return buf.getvalue(), "text/csv; charset=utf-8"


# ---------------------------------------------------------------------------
# vcf
# ---------------------------------------------------------------------------

def _vcf(conn, chat_id, media_base):
    cards = [r["vcard"].strip() for r in _fetch(conn, chat_id) if r["vcard"]]
    # Most chats never share a contact card, so empty output is the normal
    # result here, not a failure. Callers must not treat it as an error.
    if not cards:
        return "", "text/vcard; charset=utf-8"
    return "\n".join(cards) + "\n", "text/vcard; charset=utf-8"


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

def _esc(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--paper:#f7f7f5;--shell:#fff;--ink:#1a1d21;--muted:#6e7076;--faint:#9a9a9e;
--rule:#e4e4e0;--sent:#dde7e3;--sent-line:#c6d6d0;--accent:#2f6f5e;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
@media (prefers-color-scheme:dark){{:root{{--paper:#16181a;--shell:#1d2023;--ink:#e6e6e3;
--muted:#95979c;--faint:#6a6c72;--rule:#2c3033;--sent:#233530;--sent-line:#315046;--accent:#7fc0ab}}}}
*{{box-sizing:border-box}}
body{{background:var(--paper);color:var(--ink);font:400 15px/1.6 var(--sans);
margin:0;padding:28px 18px 60px}}
.sheet{{max-width:44rem;margin:0 auto}}
header{{border-bottom:1px solid var(--rule);padding-bottom:14px;margin-bottom:8px}}
header h1{{margin:0;font-size:19px}}
header .meta{{font:400 11px var(--mono);color:var(--muted);margin-top:4px}}
.day{{display:flex;align-items:center;gap:12px;margin:26px 0 14px;
font:400 10px/1 var(--mono);color:var(--faint);letter-spacing:.12em;text-transform:uppercase}}
.day::before,.day::after{{content:"";height:1px;background:var(--rule);flex:1}}
.m{{max-width:78%;margin-bottom:7px;padding:8px 11px;background:var(--shell);
border:1px solid var(--rule);border-radius:3px;break-inside:avoid}}
.m.out{{margin-left:auto;background:var(--sent);border-color:var(--sent-line)}}
.m .hd{{display:flex;justify-content:space-between;gap:14px;align-items:baseline;margin-bottom:2px}}
.m .who{{font-size:12px;font-weight:600;color:var(--accent)}}
.m .tm{{font:400 10px var(--mono);color:var(--faint);flex:none}}
.m .tx{{white-space:pre-wrap;word-break:break-word}}
.m.ev{{max-width:100%;text-align:center;background:none;border:none;color:var(--muted);
font-size:12px;font-style:italic}}
.m.gone .tx{{color:var(--faint);font-style:italic}}
.att{{margin-top:5px}}
.att img{{max-width:100%;max-height:380px;border-radius:2px;display:block}}
.att video{{max-width:100%;max-height:380px;border-radius:2px;display:block}}
.att audio{{width:100%;max-width:300px}}
.chip{{display:inline-flex;gap:7px;padding:6px 10px;border:1px solid var(--rule);
border-radius:3px;font:400 12px var(--mono);color:var(--muted);text-decoration:none}}
.chip.miss{{border-style:dashed}}
@media print{{
  :root{{--paper:#fff;--shell:#fff;--ink:#000;--sent:#eef3f1}}
  body{{padding:0}} .m{{break-inside:avoid}} .att video,.att audio{{display:none}}
}}
</style></head><body><div class="sheet">
<header><h1>{title}</h1><div class="meta">{meta}</div></header>
"""


def _html_attachment(r: sqlite3.Row, media_base: str) -> str:
    kind = r["media_kind"]
    if not kind:
        return ""
    if kind == "location" and r["latitude"] is not None:
        q = f"{r['latitude']},{r['longitude']}"
        return (f'<div class="att"><a class="chip" target="_blank" rel="noopener" '
                f'href="https://maps.google.com/?q={q}">◈ {_esc(r["caption"] or "Location")}</a></div>')
    if r["media_missing"] or not r["media_path"]:
        return f'<div class="att"><span class="chip miss">{_esc(kind)} — not in backup</span></div>'
    url = f"{media_base}/{r['media_path']}"
    if kind in ("image", "sticker"):
        return f'<div class="att"><img loading="lazy" src="{_esc(url)}" alt="{_esc(r["caption"] or kind)}"></div>'
    if kind == "video":
        return f'<div class="att"><video controls preload="metadata" src="{_esc(url)}"></video></div>'
    if kind == "audio":
        return f'<div class="att"><audio controls preload="none" src="{_esc(url)}"></audio></div>'
    return (f'<div class="att"><a class="chip" href="{_esc(url)}" download>'
            f'▤ {_esc(r["caption"] or kind)}</a></div>')


def _html(conn, chat_id, media_base):
    chat = _chat(conn, chat_id)
    rows = _fetch(conn, chat_id)
    title = chat["name"] or chat["jid"]

    span = ""
    if rows:
        span = (f"{_local(rows[0]['ts_utc']):%Y-%m-%d} → "
                f"{_local(rows[-1]['ts_utc']):%Y-%m-%d} · ")
    meta = f"{chat['kind']} · {span}{len(rows):,} messages · {_esc(chat['jid'])}"

    parts = [_HTML_HEAD.format(title=_esc(title), meta=meta)]
    last_day = None
    for r in rows:
        when = _local(r["ts_utc"])
        day = when.strftime("%Y-%m-%d")
        if day != last_day:
            parts.append(f'<div class="day">{when.strftime("%A, %d %B %Y")}</div>')
            last_day = day

        if r["event"]:
            parts.append(f'<div class="m ev">{_esc(r["event"].replace("_", " "))}</div>')
            continue

        out = r["direction"] == "out"
        body = r["body"] or ("message deleted" if r["is_deleted"] else "")
        parts.append(
            f'<div class="m{" out" if out else ""}{" gone" if r["is_deleted"] else ""}">'
            f'<div class="hd"><span class="who">{_esc(_who(r))}</span>'
            f'<span class="tm">{when.strftime("%H:%M")}</span></div>'
            f'{_html_attachment(r, media_base)}'
            f'{f"<div class=\'tx\'>{_esc(body)}</div>" if body else ""}'
            f'</div>')

    parts.append("</div></body></html>\n")
    return "".join(parts), "text/html; charset=utf-8"


_WRITERS = {"html": _html, "csv": _csv, "txt": _txt, "vcf": _vcf}


def export_chat(conn: sqlite3.Connection, chat_id: int, fmt: str = "html",
                media_base: str = "media") -> tuple[str, str]:
    """Render one chat. Returns (text, mimetype).

    `media_base` is prefixed to every attachment path: a relative folder name
    for the CLI (which copies the files next to the HTML), or `/media` for the
    reader, which serves them over HTTP.
    """
    if fmt not in _WRITERS:
        raise ValueError(f"Unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    return _WRITERS[fmt](conn, chat_id, media_base)


def media_paths(conn: sqlite3.Connection, chat_id: int) -> list[str]:
    """Vault-relative paths of every attachment in a chat, for copying alongside
    an HTML export so the folder is self-contained."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT md.path FROM media md JOIN messages m ON m.id = md.message_id "
        "WHERE m.chat_id = ? AND md.path IS NOT NULL", (chat_id,))]
