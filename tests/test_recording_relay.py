from __future__ import annotations

import unittest

from recording_relay.app import ALLOWED_API_RESOURCES, validate_recording_url


class RecordingRelayTests(unittest.TestCase):
    def test_api_proxy_has_a_narrow_resource_allowlist(self):
        self.assertEqual(ALLOWED_API_RESOURCES, {"users", "history"})

    def test_only_exact_https_megafon_host_is_allowed(self):
        valid = "https://vats671653.megapbx.ru/records/call.mp3?token=value"
        self.assertEqual(validate_recording_url(valid), valid)
        for invalid in (
            "http://vats671653.megapbx.ru/records/call.mp3",
            "https://evil.example/record.mp3",
            "https://vats671653.megapbx.ru.evil.example/record.mp3",
            "https://user:pass@vats671653.megapbx.ru/record.mp3",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_recording_url(invalid)


if __name__ == "__main__":
    unittest.main()
