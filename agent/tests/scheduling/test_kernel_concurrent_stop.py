"""Concurrent shutdown callers must not interrupt a tick's cleanup."""
import asyncio

from src.scheduling.kernel import SchedulerKernel


def test_concurrent_stops_wait_for_cleanup_without_recancelling():
    async def scenario():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cleaned = []

        async def tick(now):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.append(True)

        kernel = SchedulerKernel(on_tick=tick)
        kernel.start()
        await asyncio.wait_for(entered.wait(), 1)
        first = asyncio.create_task(kernel.stop())
        await asyncio.wait_for(cleaning.wait(), 1)
        second = asyncio.create_task(kernel.stop())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert cleaned == [True]
        assert kernel.task is None

    asyncio.run(scenario())
