"""Configuration lue dans l'environnement (variables Clever Cloud)."""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("jev_lab")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATASETS_DIR = DATA_DIR / "datasets"
CACHE_FILE = Path(os.getenv("CACHE_FILE", DATA_DIR / "cache.json"))
STATIC_DIR = ROOT / "static"


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()
JEV_BASE_URL = os.getenv("JEV_BASE_URL", "https://api.typesafe.ai/v1").rstrip("/")
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")
JEV_PRICE_IN = _float("JEV_PRICE_IN", 0.042)  # $ par million de tokens en entrée, sortie gratuite

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.mistral.ai/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "mistral-small-latest")
LLM_PRICE_IN = _float("LLM_PRICE_IN", 0.1)
LLM_PRICE_OUT = _float("LLM_PRICE_OUT", 0.3)

# Pas de code admin par défaut : sans ADMIN_CODE, l'onglet Admin est fermé.
ADMIN_CODE = os.getenv("ADMIN_CODE", "").strip()
# Classement des participants par mission (bouton « Envoyer mon score » et tableaux de l'Admin).
LEADERBOARD = os.getenv("LEADERBOARD_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
MAX_CALLS_PER_PARTICIPANT = int(_float("MAX_CALLS_PER_PARTICIPANT", 400))

# Facteur appliqué à la latence simulée du mode mock (0 dans les tests).
MOCK_LATENCY = _float("MOCK_LATENCY", 1.0)
# Appels simultanés vers chaque API, tous participants confondus.
UPSTREAM_CONCURRENCY = int(_float("UPSTREAM_CONCURRENCY", 12))

MAX_QUESTIONS = 10
MAX_CHOICE_OPTIONS = 255
MAX_STATE_CHARS = 60_000  # ~15K tokens, sous le contexte de 32K

if not os.getenv("SECRET_KEY"):
    log.warning("SECRET_KEY absente : les sessions sautent à chaque redémarrage.")
