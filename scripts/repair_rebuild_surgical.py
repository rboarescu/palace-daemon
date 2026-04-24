
import os
import sqlite3
import json
from mempalace.backends.chroma import ChromaBackend

palace_path = "/home/user/.mempalace/palace"
db_path = os.path.join(palace_path, "chroma.sqlite3")
COLLECTION_NAME = "mempalace_drawers"

def rebuild():
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Find IDs
    cursor.execute("SELECT id FROM collections WHERE name=?", (COLLECTION_NAME,))
    coll_id = cursor.fetchone()[0]
    cursor.execute("SELECT id FROM segments WHERE collection=? AND type LIKE '%sqlite%'", (coll_id,))
    seg_id = cursor.fetchone()[0]

    print(f"Collection: {coll_id}, Segment: {seg_id}")

    # 2. Extract ALL data manually from SQLite
    print("Extracting data from SQLite...")
    query = """
    SELECT e.embedding_id, m.key, m.string_value, m.int_value, m.float_value
    FROM embeddings e
    JOIN embedding_metadata m ON e.id = m.id
    WHERE e.segment_id = ?
    """
    cursor.execute(query, (seg_id,))
    
    drawers = {}
    for row in cursor.fetchall():
        eid, key, s_val, i_val, f_val = row
        if eid not in drawers:
            drawers[eid] = {"id": eid, "metadata": {}}
        
        val = s_val if s_val is not None else (i_val if i_val is not None else f_val)
        if key == "chroma:document":
            drawers[eid]["document"] = val
        else:
            drawers[eid]["metadata"][key] = val
    
    conn.close()
    
    all_drawers = list(drawers.values())
    print(f"Extracted {len(all_drawers)} drawers.")

    if not all_drawers:
        print("Nothing to rebuild.")
        return

    # 3. Clean Rebuild via Chroma API
    backend = ChromaBackend()
    print("Deleting old collection...")
    try:
        backend.delete_collection(palace_path, COLLECTION_NAME)
    except Exception as e:
        print(f"Delete failed (might not exist): {e}")

    print("Creating fresh collection...")
    col = backend.create_collection(palace_path, COLLECTION_NAME)

    print("Upserting drawers in batches...")
    batch_size = 200
    import time
    for i in range(0, len(all_drawers), batch_size):
        batch = all_drawers[i : i + batch_size]
        ids = [d["id"] for d in batch]
        docs = [d.get("document", "") for d in batch]
        metas = [d["metadata"] for d in batch]
        
        try:
            col.upsert(ids=ids, documents=docs, metadatas=metas)
            print(f"  Processed {i + len(batch)}/{len(all_drawers)}")
            time.sleep(0.5) # Slight delay to let disk/compactor breathe
        except Exception as e:
            print(f"  Batch failed at {i}: {e}. Retrying once...")
            time.sleep(2)
            col.upsert(ids=ids, documents=docs, metadatas=metas)
            print(f"  Processed {i + len(batch)}/{len(all_drawers)} (after retry)")

    print("Rebuild complete.")

if __name__ == "__main__":
    rebuild()
