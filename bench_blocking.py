import asyncio
import time
import os

async def measure_event_loop_latency():
    # start a task that counts event loop delays
    delays = []
    stop = False
    async def ticker():
        while not stop:
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            t1 = time.perf_counter()
            delays.append(t1 - t0 - 0.01)

    t = asyncio.create_task(ticker())

    # write a dummy large corrupt_ids.txt
    corrupt_file = "corrupt_ids.txt"
    with open(corrupt_file, "w") as f:
        for i in range(1000000):
            f.write(f"{i}\n")

    # the function to be tested:
    def count_lines():
        count = 0
        if os.path.isfile(corrupt_file):
            with open(corrupt_file, encoding="utf-8") as f:
                count = sum(1 for ln in f if ln.strip())
        return count

    await asyncio.sleep(0.1)

    # Test 1: baseline blocking
    delays.clear()
    t0 = time.perf_counter()
    count = count_lines()
    t1 = time.perf_counter()
    # give ticker a moment to record the delay caused by blocking
    await asyncio.sleep(0.1)

    max_delay_blocking = max(delays) if delays else 0
    print(f"Blocking read took: {t1-t0:.4f}s")
    print(f"Max event loop delay during blocking read: {max_delay_blocking:.4f}s")

    await asyncio.sleep(0.1)

    # Test 2: to_thread
    delays.clear()
    t0 = time.perf_counter()
    count = await asyncio.to_thread(count_lines)
    t1 = time.perf_counter()
    await asyncio.sleep(0.1)

    max_delay_thread = max(delays) if delays else 0
    print(f"to_thread read took: {t1-t0:.4f}s")
    print(f"Max event loop delay during to_thread read: {max_delay_thread:.4f}s")

    stop = True
    await asyncio.sleep(0.01)
    os.remove(corrupt_file)

asyncio.run(measure_event_loop_latency())
