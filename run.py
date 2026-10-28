"""Point d'entrée Clever Cloud : CC_PYTHON_UV_RUN_COMMAND=uv run python run.py (sans expansion shell)."""
import logging
import os

import uvicorn

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("lab.app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*")
