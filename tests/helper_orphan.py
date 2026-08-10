"""Spawns a detached worker_shim job and exits immediately — proves workers
survive the death of the process that dispatched them (process supervision)."""
import json
import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]

art = Path(sys.argv[1])
art.mkdir(parents=True, exist_ok=True)
(art / "job.json").write_text(json.dumps({
    "argv": [sys.executable, "-c", "import time; time.sleep(2); print('survived')"],
    "cwd": str(art), "stdin": None, "timeout": 60,
    "env_unset": [], "env_set": {}}))
DETACHED = 0x00000008 | 0x00000200 | 0x08000000
subprocess.Popen([sys.executable, str(CODE / "worker_shim.py"), str(art)],
                 creationflags=DETACHED, close_fds=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL)
# exit immediately: the shim is now an orphan
