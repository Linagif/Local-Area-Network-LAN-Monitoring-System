import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# The database lives in data/ and is never committed (see .gitignore).
# Override with the LAN_MONITOR_DB environment variable if needed.
DB_PATH = Path(os.environ.get("LAN_MONITOR_DB", BASE_DIR / "data" / "lan_monitor.db"))