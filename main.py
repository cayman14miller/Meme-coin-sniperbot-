"""Start the sniper bot + its web control panel.

    python main.py            # then open http://localhost:8000
"""
import logging

import uvicorn

from sniperbot.api import create_app
from sniperbot.config import Secrets, load_dotenv


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    secrets = Secrets.from_env()
    if secrets.mode == "live":
        print("\n  *** LIVE MODE: the bot will trade REAL funds from the configured wallet. ***\n")
    app = create_app(secrets)
    uvicorn.run(app, host=secrets.host, port=secrets.port, log_level="warning")


if __name__ == "__main__":
    main()
