import asyncio
import time
import os

path = "dummy_queue.jsonl"

def setup():
    if not os.path.exists(path):
        print("Generating dummy file...")
        with open(path, "w") as f:
            for i in range(5000000):
                f.write('{"payload": {"test": 1}}\n')

def sync_count():
    queue_path = path
    pending = 0
    if os.path.isfile(queue_path):
        try:
            with open(queue_path, encoding="utf-8") as f:
                pending = sum(1 for ln in f if ln.strip())
        except OSError:
            pending = -1
    return pending

async def async_count():
    queue_path = path
    def _count():
        if os.path.isfile(queue_path):
            try:
                with open(queue_path, encoding="utf-8") as f:
                    return sum(1 for ln in f if ln.strip())
            except OSError:
                return -1
        return 0
    return await asyncio.to_thread(_count)

async def proper_scenario(func_is_async):
    ticks = [0]
    stop = [False]
    async def ticker():
        while not stop[0]:
            await asyncio.sleep(0.01)
            ticks[0] += 1

    t = asyncio.create_task(ticker())
    start = time.time()
    if func_is_async:
        await async_count()
    else:
        # simulate calling an async endpoint that blocks synchronously
        async def wrap():
            sync_count()
        await wrap()
    duration = time.time() - start
    stop[0] = True
    await t
    # Theoretical max ticks if no blocking
    expected_ticks = int(duration / 0.01)
    print(f"Duration: {duration:.4f}s, Ticks executed: {ticks[0]} (Expected ~{expected_ticks})")
    print(f"Event loop blocked for approximately {duration - (ticks[0] * 0.01):.4f}s")

async def main():
    setup()
    print("Baseline (Synchronous I/O):")
    await proper_scenario(False)
    print("\nOptimized (asyncio.to_thread):")
    await proper_scenario(True)

if __name__ == "__main__":
    asyncio.run(main())
