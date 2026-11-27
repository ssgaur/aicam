import unittest

from runtime_policy import capture_is_due, parse_int_setting


class RuntimePolicyTests(unittest.TestCase):
    def test_parse_int_setting_uses_default_for_missing_value(self):
        self.assertEqual(
            parse_int_setting(
                None,
                name="SETTING",
                default=10,
                minimum=0,
                maximum=20,
            ),
            10,
        )

    def test_parse_int_setting_rejects_invalid_range(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 31"):
            parse_int_setting(
                "32",
                name="RETENTION_DAYS",
                default=1,
                minimum=1,
                maximum=31,
            )

    def test_capture_is_due_respects_interval_boundary(self):
        self.assertFalse(capture_is_due(100.0, 699.9, 600))
        self.assertTrue(capture_is_due(100.0, 700.0, 600))

    def test_zero_interval_accepts_every_capture(self):
        self.assertTrue(capture_is_due(100.0, 101.0, 0))


if __name__ == "__main__":
    unittest.main()
