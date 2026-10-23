"""Reads RCA reports from the reports volume and keeps human feedback next to each one.

Layout: <REPORTS_DIR>/<report id>/{report.json, rca.md, feedback.json}. feedback.json is an append-only list, the
last entry is the current verdict."""
import json
import re
import time
from pathlib import Path

from agent import config

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
VERDICTS = ("correct", "partially", "wrong")
CATEGORIES = ("dependency_latency", "dependency_errors", "pod_crash", "deploy_regression", "resource_exhaustion", "unknown")
MAX_TEXT = 2000
MAX_AUTHOR = 80
MAX_LIST = 500


class FeedbackError(ValueError):
    pass


def base() -> Path:
    return Path(config.REPORTS_DIR)


def valid_id(report_id: str) -> bool:
    return bool(ID_RE.match(report_id or ""))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _normalize(report: dict) -> dict:
    if "rca_markdown" not in report and "postmortem_markdown" in report:  # reports written before the rename
        report["rca_markdown"] = report["postmortem_markdown"]
    return report


def load_report(report_id: str) -> dict | None:
    if not valid_id(report_id):
        return None
    data = _read_json(base() / report_id / "report.json")
    return _normalize(data) if isinstance(data, dict) else None


def load_feedback(report_id: str) -> list[dict]:
    if not valid_id(report_id):
        return []
    data = _read_json(base() / report_id / "feedback.json")
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def latest_verdict(feedback: list[dict]) -> str | None:
    return feedback[-1].get("verdict") if feedback else None


def list_reports() -> list[dict]:
    """Newest first. Each report carries its feedback under the key _feedback."""
    root = base()
    if not root.is_dir():
        return []
    items = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith((".", "_")):
            continue
        report = load_report(folder.name)
        if report is None:
            continue
        report["_feedback"] = load_feedback(folder.name)
        items.append(report)
    items.sort(key=lambda r: str(r.get("started_at", "")), reverse=True)
    return items[:MAX_LIST]


def add_feedback(report_id: str, verdict: str, actual_category: str, what_fixed_it: str, author: str) -> dict:
    if load_report(report_id) is None:
        raise FeedbackError("unknown report")
    if verdict not in VERDICTS:
        raise FeedbackError("verdict must be correct, partially or wrong")
    if actual_category and actual_category not in CATEGORIES:
        raise FeedbackError("unknown category")
    fix, who = (what_fixed_it or "").strip(), (author or "").strip()
    if len(fix) > MAX_TEXT or len(who) > MAX_AUTHOR:
        raise FeedbackError("text is too long")
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": verdict,
        "actual_category": actual_category or None,
        "what_fixed_it": fix,
        "author": who or "anonymous",
    }
    folder = base() / report_id
    tmp = folder / ".feedback.json.tmp"
    tmp.write_text(json.dumps(load_feedback(report_id) + [entry], indent=2))
    tmp.replace(folder / "feedback.json")  # a reader never sees a half written file
    return entry
