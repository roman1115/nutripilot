import asyncio
import subprocess
import sys
import textwrap
import threading
from contextlib import contextmanager

import pytest

from nutripilot.domain.execution import RunExecution, current_execution, execution_fence


def test_fence_unlocks_when_default_executor_is_saturated() -> None:
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""\
            import asyncio
            import threading
            from concurrent.futures import ThreadPoolExecutor
            from contextlib import contextmanager
            from nutripilot.domain.execution import (
                RunExecution, current_execution, execution_fence,
            )

            async def main():
                asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
                lock = threading.Lock()
                waiting = threading.Event()

                @contextmanager
                def guard():
                    with lock:
                        yield

                def compete():
                    waiting.set()
                    with lock:
                        pass

                token = current_execution.set(RunExecution(fence=guard))
                try:
                    async with execution_fence():
                        competitor = asyncio.create_task(asyncio.to_thread(compete))
                        while not waiting.is_set():
                            await asyncio.sleep(0)
                        assert lock.locked()
                    await competitor
                    assert not lock.locked()
                finally:
                    current_execution.reset(token)

            asyncio.run(main())
        """)],
        check=True,
        timeout=10,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("while_entering", [False, True])
async def test_cancelled_fence_releases_ownership_lock(while_entering: bool) -> None:
    lock = threading.Lock()
    acquiring = threading.Event()
    entered = asyncio.Event()
    if while_entering:
        lock.acquire()

    @contextmanager
    def guard():
        acquiring.set()
        with lock:
            yield

    async def write():
        token = current_execution.set(RunExecution(fence=guard))
        try:
            async with execution_fence():
                entered.set()
                await asyncio.Event().wait()
        finally:
            current_execution.reset(token)

    task = asyncio.create_task(write())
    try:
        if while_entering:
            assert await asyncio.to_thread(acquiring.wait, 5)
        else:
            await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        task.cancel()
        if while_entering:
            lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lock.locked()
