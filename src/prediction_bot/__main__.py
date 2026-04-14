from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv

    # Load .env from project root (cwd) so child modules see env vars via os.getenv.
    load_dotenv(Path.cwd() / ".env", override=False)
except Exception:  # noqa: BLE001
    # python-dotenv is a soft dependency; missing import shouldn't break the CLI.
    pass

from prediction_bot.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
