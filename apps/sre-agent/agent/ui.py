"""Web UI for RCA reports: browse, read and give feedback. Server rendered, no JavaScript, no extra dependencies.

Everything that comes from a report (log lines, queries, messages) is untrusted, so it is always HTML escaped.
There is no login: anyone who can reach this page can read reports and submit feedback. Keep it behind a trusted network."""
import html
import logging
import re
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from agent import memory
from agent import reports_store as store
from agent.logging_setup import setup_logging

setup_logging("rca-ui")
log = logging.getLogger("rca-ui")
app = FastAPI(title="rca-ui", docs_url=None, redoc_url=None, openapi_url=None)

MAX_FORM_BYTES = 20_000
esc = html.escape

CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--card:#f6f8fa;--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--card:#161b22;--accent:#58a6ff}}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:1100px;margin:0 auto;padding:16px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.top{border-bottom:1px solid var(--line);background:var(--card)}header.top div{max-width:1100px;margin:0 auto;padding:12px 16px;display:flex;gap:16px;align-items:baseline}
h1{font-size:1.4rem;margin:.2em 0}h2{font-size:1.05rem;margin:1.6em 0 .4em;border-bottom:1px solid var(--line);padding-bottom:.25em}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:.85rem}
.b{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:.8rem;white-space:nowrap}
.p1{background:#cf222e;color:#fff;border-color:#cf222e}.p2{background:#bc4c00;color:#fff;border-color:#bc4c00}.p3{background:#9a6700;color:#fff;border-color:#9a6700}.p4{background:#57606a;color:#fff;border-color:#57606a}
.ok{border-color:#1a7f37;color:#1a7f37}.warn{border-color:#9a6700;color:#9a6700}.bad{border-color:#cf222e;color:#cf222e}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:10px 0}
code{font:.85em ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--card);padding:1px 5px;border-radius:4px;word-break:break-word}
form.filters{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}select,input,textarea,button{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:6px 8px}
textarea{width:100%;min-height:90px}button{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer}fieldset{border:1px solid var(--line);border-radius:8px;margin:0 0 10px}
label.r{display:inline-block;margin-right:14px}details{margin:10px 0}summary{cursor:pointer}ul{margin:.3em 0;padding-left:1.3em}
"""

PRIORITY_CLASS = {"P1": "p1", "P2": "p2", "P3": "p3", "P4": "p4"}
VERDICT_CLASS = {"correct": "ok", "partially": "warn", "wrong": "bad"}
NONE_YET = '<p class="muted">none yet</p>'


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---------------------------------------------------------------- rendering helpers

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> str:
    out = esc(text)  # escape first, then add the few tags this renderer knows, so no input can inject markup
    out = _CODE.sub(r"<code>\1</code>", out)
    return _BOLD.sub(r"<strong>\1</strong>", out)


def render_md(text: str) -> str:
    """A small, safe markdown subset: headings, bullets and checkboxes, bold, inline code, paragraphs."""
    out, in_list = [], False
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        item = re.match(r"^\s*-\s+(?:\[( |x)\]\s+)?(.*)$", line)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            box = "" if item.group(1) is None else ("&#9744; " if item.group(1) == " " else "&#9745; ")
            out.append(f"<li>{box}{_inline(item.group(2))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        head = re.match(r"^(#{1,4})\s+(.*)$", line)
        if head:
            level = len(head.group(1)) + 1
            out.append(f"<h{level}>{_inline(head.group(2))}</h{level}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        '<header class="top"><div><strong>RCA reports</strong><a href="/">All reports</a></div></header>'
        f"<main>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status)


def _when(value) -> str:
    return esc(str(value)[:19].replace("T", " ") + " UTC") if value else ""


def _priority_badge(report: dict) -> str:
    level = str((report.get("priority") or {}).get("level", "?"))
    return f'<span class="b {PRIORITY_CLASS.get(level, "")}">{esc(level)}</span>'


def _verdict_badge(feedback: list[dict]) -> str:
    verdict = store.latest_verdict(feedback)
    if not verdict:
        return '<span class="b muted">no feedback</span>'
    return f'<span class="b {VERDICT_CLASS.get(str(verdict), "")}">{esc(str(verdict))}</span>'


def _list(items) -> str:
    items = [str(i) for i in (items or [])]
    return "<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>" if items else '<p class="muted">none</p>'


def _options(values, selected: str, blank: str) -> str:
    opts = [f'<option value="">{esc(blank)}</option>']
    opts += [f'<option value="{esc(v)}"{" selected" if v == selected else ""}>{esc(v)}</option>' for v in values]
    return "".join(opts)


def _matches(report: dict, q: dict) -> bool:
    cause = report.get("root_cause") or {}
    if q["service"] and report.get("service") != q["service"]:
        return False
    if q["priority"] and (report.get("priority") or {}).get("level") != q["priority"]:
        return False
    if q["category"] and cause.get("category") != q["category"]:
        return False
    if q["verdict"] and (store.latest_verdict(report["_feedback"]) or "none") != q["verdict"]:
        return False
    if q["q"]:
        hay = " ".join(str(x) for x in (report.get("alert"), report.get("service"), report.get("summary"), cause.get("description"), report.get("id"))).lower()
        if q["q"].lower() not in hay:
            return False
    return True


# ---------------------------------------------------------------- pages

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/reports")
def api_reports():
    return [
        {
            "id": r.get("id"),
            "alert": r.get("alert"),
            "service": r.get("service"),
            "started_at": r.get("started_at"),
            "priority": (r.get("priority") or {}).get("level"),
            "layer": (r.get("fault_domain") or {}).get("layer"),
            "category": (r.get("root_cause") or {}).get("category"),
            "confidence": (r.get("root_cause") or {}).get("confidence"),
            "mode": r.get("mode"),
            "verdict": store.latest_verdict(r["_feedback"]),
        }
        for r in store.list_reports()
    ]


def _row(r: dict) -> str:
    cause = r.get("root_cause") or {}
    return (
        "<tr>"
        f'<td>{_when(r.get("started_at"))}</td><td>{_priority_badge(r)}</td>'
        f'<td><a href="/r/{esc(str(r.get("id")))}">{esc(str(r.get("alert", "?")))}</a><br><span class="muted">{esc(str(r.get("service", "")))}</span></td>'
        f'<td>{esc(str((r.get("fault_domain") or {}).get("layer", "")))}</td>'
        f'<td>{esc(str(cause.get("category", "")))}<br><span class="muted">{esc(str(cause.get("confidence", "")))} confidence, {esc(str(r.get("mode", "")))}</span></td>'
        f'<td>{_verdict_badge(r["_feedback"])}</td></tr>'
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    reports = store.list_reports()
    p = request.query_params
    q = {k: (p.get(k) or "").strip()[:100] for k in ("q", "service", "priority", "category", "verdict")}
    shown = [r for r in reports if _matches(r, q)]
    services = sorted({str(r.get("service")) for r in reports if r.get("service")})
    form = (
        '<form class="filters" method="get" action="/">'
        f'<input name="q" placeholder="search" value="{esc(q["q"])}">'
        f'<select name="service">{_options(services, q["service"], "any service")}</select>'
        f'<select name="priority">{_options(["P1", "P2", "P3", "P4"], q["priority"], "any priority")}</select>'
        f'<select name="category">{_options(store.CATEGORIES, q["category"], "any cause")}</select>'
        f'<select name="verdict">{_options(["correct", "partially", "wrong", "none"], q["verdict"], "any feedback")}</select>'
        "<button>Filter</button></form>"
    )
    if shown:
        table = (
            "<table><thead><tr><th>Started</th><th>Priority</th><th>Alert</th><th>Layer</th><th>Root cause</th><th>Feedback</th></tr></thead>"
            f'<tbody>{"".join(_row(r) for r in shown)}</tbody></table>'
        )
    else:
        table = '<p class="muted">No reports match.</p>'
    return page("RCA reports", f'<h1>RCA reports</h1><p class="muted">{len(shown)} of {len(reports)} shown</p>{form}{table}')


def _history_item(f: dict) -> str:
    out = f'<li><span class="b {VERDICT_CLASS.get(str(f.get("verdict")), "")}">{esc(str(f.get("verdict")))}</span> {esc(str(f.get("author", "")))}, {_when(f.get("at"))}'
    if f.get("actual_category"):
        out += f'<br>real cause: <code>{esc(str(f["actual_category"]))}</code>'
    if f.get("what_fixed_it"):
        out += f'<br>what fixed it: {esc(str(f["what_fixed_it"]))}'
    return out + "</li>"


def _related_item(o: dict) -> str:
    out = (
        f'<li><a href="/r/{esc(str(o.get("id")))}">{esc(str(o.get("alert")))}</a> on {esc(str(o.get("service")))}, '
        f'{esc(str((o.get("root_cause") or {}).get("category", "")))}, {_when(o.get("started_at"))} {_verdict_badge(o["_feedback"])}'
    )
    fixes = [f for f in o["_feedback"][-1:] if f.get("what_fixed_it")]
    if fixes:
        out += f'<br>what fixed it: {esc(str(fixes[0]["what_fixed_it"]))}'
    return out + "</li>"


def _feedback_form(rid: str) -> str:
    radios = "".join(f'<label class="r"><input type="radio" name="verdict" value="{v}" required> {v}</label>' for v in store.VERDICTS)
    return (
        f'<form method="post" action="/r/{esc(rid)}/feedback"><fieldset><legend>Was this diagnosis right?</legend>{radios}</fieldset>'
        f'<p><label>What was the real cause, if different?<br><select name="actual_category">{_options(store.CATEGORIES, "", "same as diagnosed")}</select></label></p>'
        f'<p><label>What fixed it?<br><textarea name="what_fixed_it" maxlength="{store.MAX_TEXT}" placeholder="the command, change or rollback that worked"></textarea></label></p>'
        f'<p><label>Your name <input name="author" maxlength="{store.MAX_AUTHOR}"></label> <button>Save feedback</button></p></form>'
    )


def _detail(report: dict, feedback: list[dict], related: list[dict]) -> str:
    rid = str(report.get("id"))
    cause, domain, prio, impact = (report.get(k) or {} for k in ("root_cause", "fault_domain", "priority", "impact"))
    fix = report.get("how_to_fix") or {}
    evidence = "".join(
        f'<tr><td>{esc(str(e.get("source", "")))}</td><td>{_inline(str(e.get("finding", "")))}</td><td><code>{esc(str(e.get("query", "")))}</code></td></tr>'
        for e in (report.get("evidence") or [])
        if isinstance(e, dict)
    )
    timeline = "".join(
        f'<li><code>{esc(str(t.get("time", "")))}</code> {_inline(str(t.get("event", "")))}</li>' for t in (report.get("timeline") or []) if isinstance(t, dict)
    )
    history = "".join(_history_item(f) for f in reversed(feedback))
    related_html = "<ul>" + "".join(_related_item(o) for o in related) + "</ul>" if related else NONE_YET
    ongoing = "Still ongoing." if prio.get("ongoing") else "No longer ongoing."
    return (
        f'<h1>{esc(str(report.get("alert")))} <span class="muted">on {esc(str(report.get("service")))}</span></h1>'
        f'<p>{_priority_badge(report)} <span class="b">{esc(str(domain.get("layer", "?")))} level</span> '
        f'<span class="b">{esc(str(cause.get("category", "?")))}, {esc(str(cause.get("confidence", "?")))} confidence</span> '
        f'<span class="b">{esc(str(report.get("mode", "")))}</span> {_verdict_badge(feedback)}<br>'
        f'<span class="muted">{esc(rid)} | started {_when(report.get("started_at"))} | <a href="/r/{esc(rid)}/rca.md">rca.md</a> | <a href="/r/{esc(rid)}/report.json">report.json</a></span></p>'
        f'<div class="card"><strong>{esc(str(prio.get("level", "")))}</strong>: respond {esc(str(prio.get("respond_within", "")))}. {ongoing}{_list(prio.get("rationale"))}</div>'
        f'<h2>What happened</h2><p>{_inline(str(report.get("what_happened", report.get("summary", ""))))}</p>'
        f'<h2>Where the problem is</h2><p><strong>{esc(str(domain.get("component", "")))}</strong></p>{_list(domain.get("reasoning"))}'
        f'<h2>Root cause</h2><p>{_inline(str(cause.get("description", "")))}</p><h3>Contributing factors</h3>{_list(report.get("contributing_factors"))}'
        f'<h2>How to fix it</h2><h3>Now</h3>{_list(fix.get("immediate"))}<h3>Permanently</h3>{_list(fix.get("permanent"))}<h3>Confirm it is fixed</h3>{_list(fix.get("verify"))}'
        f'<h2>Impact</h2><p>{_inline(str(impact.get("summary", "")))}</p>'
        f"<h2>Timeline</h2><ul>{timeline}</ul>"
        f"<h2>Evidence</h2><table><thead><tr><th>Source</th><th>Finding</th><th>Query</th></tr></thead><tbody>{evidence}</tbody></table>"
        f'<h2>Missing observability</h2>{_list(report.get("observability_gaps"))}'
        f"<h2>Similar past incidents</h2>{related_html}"
        f'<h2 id="feedback">Feedback</h2>{_feedback_form(rid)}{"<h3>History</h3><ul>" + history + "</ul>" if history else ""}'
        f'<details><summary>Full RCA text</summary>{render_md(str(report.get("rca_markdown", "")))}</details>'
    )


@app.get("/r/{report_id}", response_class=HTMLResponse)
def detail(report_id: str):
    report = store.load_report(report_id)
    if report is None:
        return page("Not found", "<h1>Report not found</h1>", 404)
    related = memory.similar(report, store.list_reports())
    return page(f'{report.get("alert", "RCA")} on {report.get("service", "")}', _detail(report, store.load_feedback(report_id), related))


@app.get("/r/{report_id}/rca.md")
def raw_markdown(report_id: str):
    report = store.load_report(report_id)
    if report is None:
        return PlainTextResponse("not found", status_code=404)
    return PlainTextResponse(str(report.get("rca_markdown", "")))


@app.get("/r/{report_id}/report.json")
def raw_json(report_id: str):
    report = store.load_report(report_id)
    if report is None:
        return PlainTextResponse("not found", status_code=404)
    return JSONResponse(report)


@app.post("/r/{report_id}/feedback")
async def post_feedback(report_id: str, request: Request):
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
        return PlainTextResponse("expected a form submission", status_code=415)
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        return PlainTextResponse("form too large", status_code=413)
    form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
    try:
        entry = store.add_feedback(report_id, form.get("verdict", ""), form.get("actual_category", ""), form.get("what_fixed_it", ""), form.get("author", ""))
    except store.FeedbackError as exc:
        return PlainTextResponse(str(exc), status_code=404 if str(exc) == "unknown report" else 400)
    log.info("rca feedback", extra={"report_id": report_id, "verdict": entry["verdict"], "actual_category": entry["actual_category"], "what_fixed_it": entry["what_fixed_it"], "by": entry["author"]})
    return RedirectResponse(f"/r/{report_id}#feedback", status_code=303)
