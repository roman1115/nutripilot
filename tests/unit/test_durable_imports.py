"""Entry points must also import correctly outside pytest's module import order."""

import subprocess
import sys


def test_standalone_durable_store_import() -> None:
    subprocess.run(
        [sys.executable, "-c", "from nutripilot.infrastructure.chat_tasks import ChatTaskStore"],
        check=True, timeout=15,
    )
