import json

import pytest
from fastapi.testclient import TestClient

from agent import config, memory, ui
from agent import reports_store as store


def make_report(rid, alert="OrdersApiHighLatencyP99", service="orders-api", category="dependency_latency", started="2026-10-23T16:24:10Z", **extra):
    report = {
        "id": rid,
        "alert": alert,
        "service": service,
        "severity": "ticket",
        "started_at": started,
        "generated_at": started,
        "mode": "rule_based",
        "summary": f"{alert} on {service}",
        "what_happened": "p99 went from 25 ms to 1990 ms",
        "priority": {"level": "P1", "respond_within": "now", "ongoing": True, "rationale": ["user facing"]},
        "fault_domain": {"layer": "application", "component": "downstream inventory.lookup", "reasoning": ["slow span"], "confidence": "high"},
        "impact": {"summary": "p99 peaked at 1990ms"},
        "timeline": [{"time": started, "event": "alert started"}],
        "root_cause": {"category": category, "description": "inventory.lookup was slow", "confidence": "high"},
        "evidence": [{"source": "mimir", "query": "histogram_quantile(0.99, x)", "finding": "p99 1990ms"}],
        "contributing_factors": ["a drill"],
        "how_to_fix": {"immediate": ["scripts/drill.sh reset"], "permanent": ["add a timeout"], "verify": ["p99 under 0.3"]},
        "observability_gaps": ["add a dependency metric"],
        "rca_markdown": "# RCA: x\n\n- [ ] do the thing\n\n**bold** and `code`",
    }
    report.update(extra)
    return report


def put(root, report):
    folder = root / report["id"]
    folder.mkdir()
    (folder / "report.json").write_text(json.dumps(report))


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(root):
    return TestClient(ui.app)


def test_list_shows_reports_newest_first_and_filters(root, client):
    put(root, make_report("rca-orders-api-1", started="2026-10-23T16:00:00Z"))
    put(root, make_report("rca-frontend-1", alert="Frontend5xx", service="frontend", category="dependency_errors", started="2026-10-23T17:00:00Z"))
    page = client.get("/").text
    assert page.index("rca-frontend-1") < page.index("rca-orders-api-1")
    only = client.get("/", params={"service": "orders-api"}).text
    assert "rca-orders-api-1" in only and "rca-frontend-1" not in only
    assert "rca-frontend-1" in client.get("/", params={"category": "dependency_errors"}).text
    assert "No reports match" in client.get("/", params={"q": "nothing-like-this"}).text


def test_detail_page_has_the_report_sections_and_feedback_form(root, client):
    put(root, make_report("rca-orders-api-1"))
    page = client.get("/r/rca-orders-api-1").text
    for expected in ("What happened", "How to fix it", "scripts/drill.sh reset", "Evidence", "Missing observability", "Was this diagnosis right?", "Full RCA text"):
        assert expected in page


def test_untrusted_text_is_escaped_everywhere(root, client):
    evil = "<script>alert(1)</script>"
    put(root, make_report("rca-orders-api-1", what_happened=evil, rca_markdown=f"# {evil}\n\n- {evil}", evidence=[{"source": evil, "query": evil, "finding": evil}], summary=evil))
    for url in ("/r/rca-orders-api-1", "/"):
        text = client.get(url).text
        assert "<script>" not in text
    assert "&lt;script&gt;" in client.get("/r/rca-orders-api-1").text


def test_unknown_and_malformed_ids_are_404(root, client):
    assert client.get("/r/does-not-exist").status_code == 404
    assert client.get("/r/NOT_A_VALID_ID").status_code == 404
    assert client.get("/r/..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/r/does-not-exist/rca.md").status_code == 404


def test_reports_written_before_the_rename_still_render(root, client):
    legacy = make_report("rca-orders-api-1")
    legacy["postmortem_markdown"] = legacy.pop("rca_markdown")
    put(root, legacy)
    assert "do the thing" in client.get("/r/rca-orders-api-1").text
    assert "do the thing" in client.get("/r/rca-orders-api-1/rca.md").text


def test_feedback_is_saved_shown_and_appended(root, client):
    put(root, make_report("rca-orders-api-1"))
    resp = client.post("/r/rca-orders-api-1/feedback", data={"verdict": "wrong", "actual_category": "deploy_regression", "what_fixed_it": "rolled back", "author": "kk"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/r/rca-orders-api-1#feedback"
    saved = json.loads((root / "rca-orders-api-1" / "feedback.json").read_text())
    assert saved[0]["verdict"] == "wrong" and saved[0]["actual_category"] == "deploy_regression" and saved[0]["author"] == "kk"
    client.post("/r/rca-orders-api-1/feedback", data={"verdict": "correct", "what_fixed_it": "reset the drill"}, follow_redirects=False)
    assert len(json.loads((root / "rca-orders-api-1" / "feedback.json").read_text())) == 2  # history is kept
    page = client.get("/r/rca-orders-api-1").text
    assert "rolled back" in page and "reset the drill" in page
    assert client.get("/api/reports").json()[0]["verdict"] == "correct"  # the latest entry is the verdict
    assert "correct" in client.get("/", params={"verdict": "correct"}).text


@pytest.mark.parametrize(
    "data,status",
    [
        ({"verdict": "maybe"}, 400),
        ({"verdict": "correct", "actual_category": "made_up"}, 400),
        ({"verdict": "correct", "what_fixed_it": "x" * (store.MAX_TEXT + 1)}, 400),
        ({"verdict": "correct", "author": "x" * (store.MAX_AUTHOR + 1)}, 400),
    ],
)
def test_bad_feedback_is_rejected_and_not_saved(root, client, data, status):
    put(root, make_report("rca-orders-api-1"))
    assert client.post("/r/rca-orders-api-1/feedback", data=data, follow_redirects=False).status_code == status
    assert not (root / "rca-orders-api-1" / "feedback.json").exists()


def test_feedback_for_an_unknown_report_is_404(root, client):
    assert client.post("/r/nope/feedback", data={"verdict": "correct"}, follow_redirects=False).status_code == 404


def test_feedback_refuses_cross_origin_wrong_type_and_big_bodies(root, client):
    put(root, make_report("rca-orders-api-1"))
    url = "/r/rca-orders-api-1/feedback"
    assert client.post(url, data={"verdict": "correct"}, headers={"origin": "http://evil.example"}, follow_redirects=False).status_code == 403
    assert client.post(url, data={"verdict": "correct"}, headers={"origin": "http://testserver"}, follow_redirects=False).status_code == 303
    assert client.post(url, json={"verdict": "correct"}, follow_redirects=False).status_code == 415
    assert client.post(url, content="verdict=correct&what_fixed_it=" + "x" * (ui.MAX_FORM_BYTES + 1), headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False).status_code == 413


def test_security_headers_are_set(root, client):
    resp = client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_markdown_renderer_supports_a_small_safe_subset():
    out = ui.render_md("# Title\n\n- [ ] open\n- [x] done\n- plain\n\n**bold** and `<b>code</b>`")
    assert "<h2>Title</h2>" in out and "&#9744; open" in out and "&#9745; done" in out
    assert "<strong>bold</strong>" in out and "<code>&lt;b&gt;code&lt;/b&gt;</code>" in out


def test_similar_incidents_rank_the_same_alert_and_service_first():
    a = make_report("a")
    same = make_report("b", started="2026-10-22T10:00:00Z")
    other_alert = make_report("c", alert="OrdersApiErrorBudgetFastBurn")
    unrelated = make_report("d", alert="PodOOMKilled", service="argocd-application-controller", category="resource_exhaustion", fault_domain={"layer": "infrastructure", "component": "memory limit", "reasoning": [], "confidence": "high"})
    found = memory.similar(a, [a, same, other_alert, unrelated])
    assert [r["id"] for r in found][0] == "b"
    assert "a" not in [r["id"] for r in found] and "d" not in [r["id"] for r in found]


def test_list_ignores_hidden_and_broken_folders(root, client):
    put(root, make_report("rca-orders-api-1"))
    (root / "_memory").mkdir()
    (root / ".tmp").mkdir()
    (root / "rca-broken").mkdir()
    (root / "rca-broken" / "report.json").write_text("{not json")
    assert [r["id"] for r in client.get("/api/reports").json()] == ["rca-orders-api-1"]
