"""Jev Lab : API FastAPI + page unique servie en statique."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, cache, config, providers
from .questions import build_request

log = logging.getLogger("jev_lab")


def load_datasets() -> dict[str, dict]:
    out = {}
    for f in sorted(config.DATASETS_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        out[d["id"]] = d
    return out


DATASETS = load_datasets()
PRESETS = json.loads((config.DATA_DIR / "presets.json").read_text())

# Statistiques par participant (mémoire du processus, suffisant pour un atelier).
STATS: dict[str, dict] = {}
# Meilleur score par mission et par participant.
SCORES: dict[str, dict[str, dict]] = {}
WARM = {"running": False, "done": 0, "total": 0, "errors": 0, "last_error": ""}


@asynccontextmanager
async def lifespan(_: FastAPI):
    cache.load()
    log.info("Jev %s, LLM %s, %d jeux de cas",
             "MOCK" if providers.jev_is_mock() else config.JEV_MODEL,
             "MOCK" if providers.llm_is_mock() else config.LLM_MODEL, len(DATASETS))
    yield
    if providers._client:
        await providers._client.aclose()


app = FastAPI(title="Jev Lab", lifespan=lifespan, docs_url=None, redoc_url=None)


def _stats(name: str) -> dict:
    return STATS.setdefault(name, {"name": name, "jev": 0, "llm": 0, "cached": 0, "errors": 0,
                                   "tokens": 0, "cost_usd": 0.0, "last_seen": 0})


async def run_call(provider: str, request: dict, user: dict | None) -> dict:
    is_jev = provider == "jev"
    mock = providers.jev_is_mock() if is_jev else providers.llm_is_mock()
    model = config.JEV_MODEL if is_jev else config.LLM_MODEL
    st = _stats(user["name"]) if user else None
    if st:
        st["last_seen"] = int(time.time())

    k = cache.key(provider, model, request)
    hit = None if mock else cache.get(k)
    if hit:
        if st:
            st["cached"] += 1
        result, latency, cached = hit["result"], hit["latency_ms"], True
    else:
        if not mock and cache.cache_only:
            raise HTTPException(503, "Mode « cache seul » : ce cas n'a pas été préchauffé. "
                                     "Reviens aux questions d'origine du jeu de cas.")
        if st and st["jev"] + st["llm"] >= config.MAX_CALLS_PER_PARTICIPANT:
            raise HTTPException(429, f"Quota atteint ({config.MAX_CALLS_PER_PARTICIPANT} appels). "
                                     "Les cas déjà en cache restent disponibles.")
        t0 = providers.now_ms()
        try:
            result = await (providers.call_jev(request) if is_jev else providers.call_llm(request))
        except providers.UpstreamError as e:
            if st:
                st["errors"] += 1
            raise HTTPException(502, str(e)) from e
        latency, cached = round(providers.now_ms() - t0), False
        if st:
            st[provider] += 1
        if not mock:
            await cache.put(k, {"provider": provider, "result": result, "latency_ms": latency})

    cost = providers.jev_cost(result["usage"]) if is_jev else providers.llm_cost(result["usage"])
    if st and not cached:
        st["tokens"] += result["usage"].get("input_tokens", 0)
        st["cost_usd"] += cost
    return {**result, "provider": provider, "latency_ms": latency, "cost_usd": cost,
            "cached": cached, "mock": mock}


# --------------------------------------------------------------------------- API


class Login(BaseModel):
    code: str = ""
    name: str = ""


class Ask(BaseModel):
    state: Any
    questions: dict[str, Any]
    guard: bool = True


@app.post("/api/login")
def login(body: Login):
    return auth.login(body.code, body.name)


@app.get("/api/config")
def get_config(user: dict = Depends(auth.current_user)):
    st = STATS.get(user["name"], {})
    return {
        "user": user,
        "jev": {"mock": providers.jev_is_mock(), "model": config.JEV_MODEL, "price_in": config.JEV_PRICE_IN},
        "llm": {"mock": providers.llm_is_mock(), "model": config.LLM_MODEL,
                "price_in": config.LLM_PRICE_IN, "price_out": config.LLM_PRICE_OUT},
        "max_questions": config.MAX_QUESTIONS,
        "quota": config.MAX_CALLS_PER_PARTICIPANT,
        "used": st.get("jev", 0) + st.get("llm", 0),
        "cache_only": cache.cache_only,
        "leaderboard": config.LEADERBOARD,
    }


@app.get("/api/presets")
def get_presets(_: dict = Depends(auth.current_user)):
    return PRESETS


@app.get("/api/datasets")
def list_datasets(_: dict = Depends(auth.current_user)):
    return [{k: d[k] for k in ("id", "title", "description", "kind", "mission")}
            | {"n": len(d["cases"]), "custom": d.get("custom", False), "template": d.get("template")}
            for d in DATASETS.values()]


@app.get("/api/datasets/{ds_id}")
def get_dataset(ds_id: str, _: dict = Depends(auth.current_user)):
    if ds_id not in DATASETS:
        raise HTTPException(404, "Jeu de cas inconnu.")
    return DATASETS[ds_id]


@app.post("/api/jev")
async def ask_jev(body: Ask, user: dict = Depends(auth.current_user)):
    request = build_request(body.state, body.questions, body.guard)
    return {**await run_call("jev", request, user), "request": request}


@app.post("/api/llm")
async def ask_llm(body: Ask, user: dict = Depends(auth.current_user)):
    request = build_request(body.state, body.questions, body.guard)
    return {**await run_call("llm", request, user), "request": {"model": config.LLM_MODEL,
                                                                 "messages": providers.llm_messages(request)}}


# --------------------------------------------------------------------------- Classement


class Score(BaseModel):
    dataset: str
    cost: float
    fn: int = 0
    fp: int = 0
    human: int = 0
    wrong: int = 0
    automation: float = 0
    settings: str = ""
    modified: bool = False


def _better(a: dict, b: dict | None) -> bool:
    return b is None or (a["cost"], a["fn"] + a["wrong"]) < (b["cost"], b["fn"] + b["wrong"])


def leaderboard(ds_id: str, limit: int | None = 20) -> list[dict]:
    rows = SCORES.get(ds_id, {}).values()
    return sorted(rows, key=lambda r: (r["cost"], r["fn"] + r["wrong"], r["at"]))[:limit]


@app.post("/api/score")
def post_score(body: Score, user: dict = Depends(auth.current_user)):
    if not config.LEADERBOARD:
        raise HTTPException(404, "Classement désactivé.")
    if body.dataset not in DATASETS or DATASETS[body.dataset].get("custom"):
        raise HTTPException(404, "Pas de classement pour cette mission.")
    entry = body.model_dump() | {"name": user["name"], "at": int(time.time())}
    board = SCORES.setdefault(body.dataset, {})
    improved = _better(entry, board.get(user["name"]))
    if improved:
        board[user["name"]] = entry
    rank = [r["name"] for r in leaderboard(body.dataset, None)].index(user["name"]) + 1
    return {"improved": improved, "best": board[user["name"]], "rank": rank, "board": leaderboard(body.dataset)}


@app.get("/api/leaderboard/{ds_id}")
def get_leaderboard(ds_id: str, _: dict = Depends(auth.current_user)):
    if not config.LEADERBOARD:
        raise HTTPException(404, "Classement désactivé.")
    return leaderboard(ds_id)


# --------------------------------------------------------------------------- Admin


@app.get("/api/admin/stats")
def admin_stats(_: dict = Depends(auth.admin_user)):
    rows = sorted(STATS.values(), key=lambda s: -(s["jev"] + s["llm"] + s["cached"]))
    boards = {d["id"]: {"title": d["title"], "kind": d["kind"], "rows": leaderboard(d["id"])}
              for d in DATASETS.values() if config.LEADERBOARD and not d.get("custom")}
    return {"participants": rows, "cache_size": cache.size(), "cache_only": cache.cache_only,
            "warm": WARM, "quota": config.MAX_CALLS_PER_PARTICIPANT, "leaderboards": boards}


class Toggle(BaseModel):
    on: bool


@app.post("/api/admin/cache-only")
def admin_cache_only(body: Toggle, _: dict = Depends(auth.admin_user)):
    cache.cache_only = body.on
    return {"cache_only": cache.cache_only}


@app.get("/api/admin/cache.json")
def admin_cache_file(_: dict = Depends(auth.admin_user)):
    return Response(cache.dump(), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="cache.json"'})


async def _warm() -> None:
    jobs = [(p, build_request(c["text"], d["questions"], True))
            for d in DATASETS.values() if not d.get("custom") for c in d["cases"] for p in ("jev", "llm")]
    WARM.update(running=True, done=0, total=len(jobs), errors=0, last_error="")
    sem = asyncio.Semaphore(4)

    async def one(provider: str, request: dict) -> None:
        async with sem:
            try:
                await run_call(provider, request, None)
            except HTTPException as e:
                WARM["errors"] += 1
                WARM["last_error"] = f"{provider} : {e.detail}"
            WARM["done"] += 1

    try:
        await asyncio.gather(*(one(p, r) for p, r in jobs))
    finally:
        WARM["running"] = False


@app.post("/api/admin/warm")
async def admin_warm(_: dict = Depends(auth.admin_user)):
    if WARM["running"]:
        return WARM
    if cache.cache_only:
        raise HTTPException(409, "Coupe le mode « cache seul » avant de préchauffer.")
    app.state.warm_task = asyncio.create_task(_warm())
    await asyncio.sleep(0)
    return WARM


# --------------------------------------------------------------------------- Page


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    # Le numéro de version force les navigateurs à recharger le script et le style après chaque déploiement.
    v = max(int((config.STATIC_DIR / f).stat().st_mtime) for f in ("app.js", "style.css"))
    html = (config.STATIC_DIR / "index.html").read_text()
    html = html.replace("/static/app.js", f"/static/app.js?v={v}").replace("/static/style.css", f"/static/style.css?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
