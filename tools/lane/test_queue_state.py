import json
import tempfile
import unittest
from pathlib import Path

import queue_state


class QueueStateTests(unittest.TestCase):
    def test_recovery_blocks_ambiguous_work_and_preserves_done(self):
        state = {"items": [
            {"id": "a", "status": "done", "kind": "x", "note": ""},
            {"id": "b", "status": "in_progress", "kind": "x", "note": ""},
            {"id": "c", "status": "pending", "kind": "x", "note": ""},
        ]}
        self.assertTrue(queue_state.recover(state))
        self.assertEqual(state["items"][0]["status"], "done")
        self.assertEqual(state["items"][1]["status"], "blocked")
        self.assertEqual(queue_state.next_item(state)["id"], "c")

    def test_round_trip_is_atomic_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            state = {"items": [{"id": "one", "kind": "work", "status": "pending", "note": ""}]}
            queue_state.save(path, state)
            self.assertEqual(queue_state.load(path), state)


if __name__ == "__main__":
    unittest.main()
