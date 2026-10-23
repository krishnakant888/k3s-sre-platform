"""Finds past incidents that look like a given one. Plain token overlap, no dependencies, no network."""
import re

_STOP = {"the", "a", "of", "on", "in", "and", "to", "is", "level"}


def _tokens(report: dict) -> set[str]:
    cause = report.get("root_cause") or {}
    domain = report.get("fault_domain") or {}
    text = " ".join(str(x) for x in (report.get("alert", ""), report.get("service", ""), cause.get("category", ""), domain.get("layer", ""), domain.get("component", "")))
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return set(re.findall(r"[a-z0-9]+", text.lower())) - _STOP


def similar(report: dict, others: list[dict], k: int = 3) -> list[dict]:
    """The k most similar other incidents, best first. Same alert, service and root cause category all raise the score."""
    mine = _tokens(report)
    scored = []
    for other in others:
        if other.get("id") == report.get("id"):
            continue
        theirs = _tokens(other)
        union = mine | theirs
        if not union:
            continue
        score = len(mine & theirs) / len(union)
        score += 0.3 * (other.get("alert") == report.get("alert"))
        score += 0.2 * (other.get("service") == report.get("service"))
        score += 0.2 * ((other.get("root_cause") or {}).get("category") == (report.get("root_cause") or {}).get("category"))
        if score > 0.35:
            scored.append((score, other))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [o for _, o in scored[:k]]
