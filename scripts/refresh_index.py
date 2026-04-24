
import os
import time
from mempalace.backends.chroma import ChromaBackend

palace_path = "/home/user/.mempalace/palace"
COLLECTION_NAME = "mempalace_drawers"
batch_size = 500

backend = ChromaBackend()
col = backend.get_collection(palace_path, COLLECTION_NAME)
total = col.count()

print(f"Starting refresh of {total} drawers...")

for offset in range(0, total, batch_size):
    try:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas", "embeddings"])
        if not batch["ids"]:
            print(f"No more IDs found at offset {offset}")
            break
        
        kwargs = {
            "ids": batch["ids"],
            "documents": batch["documents"],
            "metadatas": batch["metadatas"]
        }
        # Check if embeddings actually contain data (not just a list of Nones)
        if batch.get("embeddings") is not None and len(batch["embeddings"]) > 0:
             # Chroma returns list of lists
             kwargs["embeddings"] = batch["embeddings"]
             
        col.upsert(**kwargs)
        print(f"Processed {offset + len(batch['ids'])}/{total}")
    except Exception as e:
        print(f"Error at offset {offset}: {e}")
        time.sleep(1)

print("Refresh complete.")
