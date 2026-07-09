1. Modify `main.py` to use `asyncio.to_thread` for reading the `corrupt_ids.txt` file in `mode == "scan"`.
2. I will write a benchmark to measure event loop latency, and show that blocking reads pause the loop, while `to_thread` preserves event loop latency.
3. Pre-commit check steps.
4. Submit PR.
