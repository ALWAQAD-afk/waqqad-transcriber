import unittest

from update_gate import SeenUpdates, is_stale_message


class TestSeenUpdates(unittest.TestCase):
    def test_new_update_is_not_seen(self):
        seen = SeenUpdates()
        self.assertFalse(seen.seen(1))

    def test_same_update_is_seen_again(self):
        seen = SeenUpdates()
        self.assertFalse(seen.seen(1))
        self.assertTrue(seen.seen(1))

    def test_none_is_never_seen(self):
        seen = SeenUpdates()
        self.assertFalse(seen.seen(None))
        self.assertFalse(seen.seen(None))

    def test_old_ids_are_evicted(self):
        seen = SeenUpdates(limit=2)
        seen.seen(1)
        seen.seen(2)
        seen.seen(3)
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen.seen(1))

    def test_clear(self):
        seen = SeenUpdates()
        seen.seen(5)
        seen.clear()
        self.assertFalse(seen.seen(5))


class TestStaleMessage(unittest.TestCase):
    def test_old_message_is_stale(self):
        update = {"message": {"date": 1000}}
        self.assertTrue(is_stale_message(update, max_age_seconds=300, now=2000))

    def test_fresh_message_is_not_stale(self):
        update = {"message": {"date": 1900}}
        self.assertFalse(is_stale_message(update, max_age_seconds=300, now=2000))

    def test_missing_date_is_not_stale(self):
        self.assertFalse(is_stale_message({"message": {}}))

    def test_callback_query_is_never_stale(self):
        update = {
            "callback_query": {
                "message": {"date": 1000},
                "data": "fb:good",
            }
        }
        self.assertFalse(is_stale_message(update, max_age_seconds=300, now=9999))


if __name__ == "__main__":
    unittest.main()
