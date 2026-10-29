import json
import os

os.environ.update(ADMIN_CODE="chef", SECRET_KEY="test", MOCK_LATENCY="0",
                  MAX_CALLS_PER_PARTICIPANT="5", TYPESAFE_API_KEY="", LLM_API_KEY="")
os.environ["CACHE_FILE"] = "/tmp/jevlab-test-cache.json"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lab import app as appmod, cache, config, providers  # noqa: E402

QS = {
    "department": {"type": "choice", "instructions": "Which team should handle this",
                   "criteria": {"billing": "Payment issues", "technical": "Bugs", "other": "Anything else"}},
    "frustration": {"type": "score", "instructions": "How frustrated", "criteria": ["Calm", "Civil", "Angry"]},
    "is_urgent": {"type": "noul", "instructions": "The message conveys urgency"},
}


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    appmod.STATS.clear()
    appmod.SCORES.clear()
    cache._entries.clear()
    cache.cache_only = False
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    if os.path.exists(config.CACHE_FILE):
        os.remove(config.CACHE_FILE)


@pytest.fixture
def client():
    with TestClient(appmod.app) as c:
        yield c


def login(c, code="", name="Binôme 1"):
    r = c.post("/api/login", json={"code": code, "name": name})
    assert r.status_code == 200, r.text
    return {"X-Lab-Token": r.json()["token"]}


def test_login(client):
    assert client.post("/api/login", json={"code": "nope", "name": "x"}).status_code == 401
    assert client.post("/api/login", json={"name": " "}).status_code == 400
    assert client.post("/api/login", json={"name": "Léa"}).json()["role"] == "participant"
    h = login(client, "chef", "")
    cfg = client.get("/api/config", headers=h).json()
    assert cfg["user"]["role"] == "admin" and cfg["jev"]["mock"] and cfg["llm"]["mock"]
    assert client.get("/api/config", headers={"X-Lab-Token": "faux.jeton"}).status_code == 401
    assert client.get("/api/admin/stats", headers=login(client)).status_code == 403


def test_jev_mock_shape_and_guard(client):
    h = login(client)
    r = client.post("/api/jev", headers=h, json={"state": "The app crashes, urgent!", "questions": QS})
    assert r.status_code == 200, r.text
    d = r.json()
    a = d["answers"]
    assert a["department"]["choice"] in QS["department"]["criteria"]
    assert abs(sum(a["department"]["probabilities"].values()) - 1) < 0.05
    assert 0 <= a["frustration"]["score"] <= 2 and 0 <= a["is_urgent"]["noul"] <= 1
    assert d["mock"] and not d["cached"] and d["model"] == "jev-mock"
    assert "non fiable" in d["request"]["questions"]["is_urgent"]["instructions"]
    r2 = client.post("/api/jev", headers=h, json={"state": "The app crashes, urgent!", "questions": QS, "guard": False})
    assert "non fiable" not in r2.json()["request"]["questions"]["is_urgent"]["instructions"]
    assert r2.json()["answers"]["department"] == a["department"]  # déterministe


def test_validation(client):
    h = login(client)
    too_many = {f"q{i}": {"type": "noul", "instructions": "x"} for i in range(11)}
    assert client.post("/api/jev", headers=h, json={"state": "x", "questions": too_many}).status_code == 422
    bad = {"Bad Name": {"type": "noul", "instructions": "x"}}
    assert client.post("/api/jev", headers=h, json={"state": "x", "questions": bad}).status_code == 422
    one_opt = {"c": {"type": "choice", "instructions": "x", "criteria": {"a": "a"}}}
    assert client.post("/api/jev", headers=h, json={"state": "x", "questions": one_opt}).status_code == 422
    assert client.post("/api/jev", headers=h, json={"state": "  ", "questions": QS}).status_code == 422


def test_quota(client):
    h = login(client, name="glouton")
    for i in range(5):
        assert client.post("/api/jev", headers=h, json={"state": f"cas {i}", "questions": QS}).status_code == 200
    r = client.post("/api/llm", headers=h, json={"state": "un de trop", "questions": QS})
    assert r.status_code == 429
    assert client.get("/api/config", headers=h).json()["used"] == 5


def test_llm_normalize_type_errors():
    content = json.dumps({"answers": {
        "department": {"choice": "billing_or_other", "confidence": 0.9},
        "frustration": {"level": 2.0},
        "is_urgent": {"probability": 1.4}}, "explanation": "Parce que."})
    answers, expl, errors = providers.normalize_llm(content, QS)
    assert list(answers) == ["frustration"] and answers["frustration"]["score"] == 2.0
    assert len(errors) == 2 and expl == "Parce que."
    assert providers.normalize_llm("pas du json", QS)[2] == ["JSON invalide"]
    ok = json.dumps({"answers": {"department": {"choice": "technical", "confidence": 0.7},
                                 "frustration": {"level": 0}, "is_urgent": {"probability": 0.2}}})
    a, _, e = providers.normalize_llm(ok, QS)
    assert not e and a["department"]["probabilities"]["technical"] == 0.7
    assert abs(a["department"]["probabilities"]["billing"] - 0.15) < 1e-9


def test_llm_mock(client):
    h = login(client)
    d = client.post("/api/llm", headers=h, json={"state": "Facture en double", "questions": QS}).json()
    assert d["provider"] == "llm" and d["explanation"] and "messages" in d["request"]
    assert d["cost_usd"] > 0


def _fake_upstream(calls):
    def handler(req: httpx.Request):
        calls.append(str(req.url))
        assert req.headers["authorization"] == "Bearer k"
        body = json.loads(req.content)
        if req.url.path.endswith("/systemone"):
            return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 1000, "output_tokens": 9},
                                             "answers": {n: {"type": "noul", "noul": 0.9} for n in body["questions"]}})
        content = json.dumps({"answers": {}, "explanation": "ok"})
        return httpx.Response(200, json={"model": "m", "choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    return handler


def test_real_jev_cache_and_cache_only(client, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(providers, "_client", httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream(calls))))
    h = login(client)
    qs = {"is_urgent": QS["is_urgent"]}
    d1 = client.post("/api/jev", headers=h, json={"state": "help", "questions": qs}).json()
    assert d1["model"] == "jev-1.13.0" and not d1["cached"] and not d1["mock"]
    assert abs(d1["cost_usd"] - 0.042 / 1000) < 1e-12
    d2 = client.post("/api/jev", headers=h, json={"state": "help", "questions": qs}).json()
    assert d2["cached"] and d2["latency_ms"] == d1["latency_ms"] and len(calls) == 1
    assert json.loads(open(config.CACHE_FILE).read())  # persisté
    assert client.get("/api/config", headers=h).json()["used"] == 1  # le cache ne compte pas

    adm = login(client, "chef", "anim")
    assert client.post("/api/admin/cache-only", headers=adm, json={"on": True}).json()["cache_only"]
    assert client.post("/api/jev", headers=h, json={"state": "help", "questions": qs}).status_code == 200
    assert client.post("/api/jev", headers=h, json={"state": "nouveau", "questions": qs}).status_code == 503
    dump = client.get("/api/admin/cache.json", headers=adm)
    assert dump.status_code == 200 and len(dump.json()) == 1
    stats = client.get("/api/admin/stats", headers=adm).json()
    assert stats["participants"][0]["cached"] == 2


def test_upstream_error(client, monkeypatch):
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(providers, "_client", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"}))))
    r = client.post("/api/jev", headers=login(client), json={"state": "x", "questions": QS})
    assert r.status_code == 502 and "401" in r.json()["detail"]


def test_datasets_and_warm(client):
    h = login(client)
    ds = client.get("/api/datasets", headers=h).json()
    assert {d["id"] for d in ds} >= {"anti-arnaque", "routage-tickets"}
    for d in ds:
        full = client.get(f"/api/datasets/{d['id']}", headers=h).json()
        assert len(full["cases"]) >= (10 if d["custom"] else 20) and len(full["questions"]) <= 10
        labels = [full["labels"]["positive"], full["labels"]["negative"]] if full["kind"] == "binary" else full["labels"]
        assert all(c["label"] in labels for c in full["cases"])
        if full["kind"] == "multiclass":
            assert set(labels) == set(full["questions"][full["target"]["question"]]["criteria"])
    ch = client.get("/api/datasets/anti-arnaque", headers=h).json()["challenge"]
    assert ch["case"] < 22 and "trompée de numéro" in client.get("/api/datasets/anti-arnaque", headers=h).json()["cases"][ch["case"]]["text"]
    adm = login(client, "chef", "anim")
    assert client.post("/api/admin/warm", headers=adm).status_code == 200


def test_leaderboard_off_by_default(client):
    h = login(client)
    assert client.get("/api/config", headers=h).json()["leaderboard"] is False
    assert client.post("/api/score", headers=h, json={"dataset": "anti-arnaque", "cost": 3}).status_code == 404
    assert client.get("/api/leaderboard/anti-arnaque", headers=h).status_code == 404
    assert client.get("/api/admin/stats", headers=login(client, "chef", "anim")).json()["leaderboards"] == {}


def test_leaderboard_keeps_best(client, monkeypatch):
    monkeypatch.setattr(config, "LEADERBOARD", True)
    a, b = login(client, name="A"), login(client, name="B")
    post = lambda h, cost, fn=0: client.post("/api/score", headers=h, json={"dataset": "anti-arnaque", "cost": cost, "fn": fn})
    assert post(a, 8).json()["rank"] == 1
    assert post(b, 5).json()["rank"] == 1
    r = post(a, 9).json()
    assert not r["improved"] and r["best"]["cost"] == 8 and r["rank"] == 2
    assert [x["name"] for x in client.get("/api/leaderboard/anti-arnaque", headers=a).json()] == ["B", "A"]
    assert client.post("/api/score", headers=a, json={"dataset": "votre-cas-alerte", "cost": 1}).status_code == 404
    boards = client.get("/api/admin/stats", headers=login(client, "chef", "anim")).json()["leaderboards"]
    assert "votre-cas-alerte" not in boards and len(boards["anti-arnaque"]["rows"]) == 2


def test_index_served(client):
    assert "Jev Lab" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
