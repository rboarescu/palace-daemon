import time
import os
import sys
from unittest.mock import patch, MagicMock

# Add scripts to path
sys.path.insert(0, os.path.abspath('.'))

import scripts.repair_rebuild_surgical as rs

def run_benchmark():
    # Mock data
    num_drawers = 1000
    mock_drawers = []
    for i in range(num_drawers):
        mock_drawers.append((f"id_{i}", "chroma:document", f"doc_{i}", None, None))

    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [(1,), (2,)]
    mock_cursor.fetchall.return_value = mock_drawers

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_col = MagicMock()

    mock_backend = MagicMock()
    mock_backend.create_collection.return_value = mock_col

    with patch('sqlite3.connect', return_value=mock_conn), \
         patch('scripts.repair_rebuild_surgical.ChromaBackend', return_value=mock_backend):

        start = time.time()
        rs.rebuild()
        end = time.time()

    return end - start

if __name__ == "__main__":
    duration = run_benchmark()
    print(f"Benchmark finished in {duration:.4f} seconds")
