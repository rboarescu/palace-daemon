import requests
import json
import time

url = "http://localhost:8085/memory"
wing = "stress_test"
room = "load_test"

for i in range(1, 11):
    content = f"Stress test drawer {i} - timestamp: {time.time()}"
    payload = {
        "content": content,
        "wing": wing,
        "room": room
    }
    print(f"Starting write {i}...")
    start_time = time.time()
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        end_time = time.time()
        print(f"Finished write {i} in {end_time - start_time:.2f} seconds. Response: {response.text}")
    except Exception as e:
        print(f"Error during write {i}: {e}")
        break
    # No artificial delay, "rapid writes" as requested, but waiting for confirmation.
