from datetime import datetime, timezone
import unittest


class TravelDateResolutionTests(unittest.TestCase):
    def resolve(self, start, end, zone, now=None):
        try:
            from olcr_api.travel_dates import resolve_trip_dates
        except ModuleNotFoundError:
            self.fail("travel date resolution is not implemented")
        return resolve_trip_dates(start, end, zone, now)

    def test_partial_date_uses_client_zone_and_computes_next_occurrence(self):
        result = self.resolve(
            "12/12", "12/15", "Asia/Tokyo",
            datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(result.start_date, "2026-12-12")
        self.assertEqual(result.end_date, "2026-12-15")
        self.assertIsNone(result.clarification)

    def test_missing_or_invalid_zone_asks_for_explicit_year(self):
        for zone in (None, "Mars/Olympus_Mons"):
            with self.subTest(zone=zone):
                result = self.resolve(
                    "12/12", "12/15", zone,
                    datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc),
                )
                self.assertIsNone(result.start_date)
                self.assertIsNone(result.end_date)
                self.assertIn("年", result.clarification)

    def test_invalid_calendar_date_is_rejected(self):
        result = self.resolve("2026-02-30", "2026-03-03", "Asia/Tokyo")
        self.assertIsNone(result.start_date)
        self.assertIsNone(result.end_date)
        self.assertIsNotNone(result.clarification)

    def test_explicit_iso_dates_do_not_depend_on_client_timezone(self):
        result = self.resolve("2026-12-12", "2026-12-15", None)
        self.assertEqual(result.start_date, "2026-12-12")
        self.assertEqual(result.end_date, "2026-12-15")
        self.assertIsNone(result.clarification)

    def test_japanese_full_date_is_parsed_without_timezone(self):
        result = self.resolve("2026年12月12日", "2026年12月15日", None)
        self.assertEqual(result.start_date, "2026-12-12")
        self.assertEqual(result.end_date, "2026-12-15")
        self.assertIsNone(result.clarification)


if __name__ == "__main__":
    unittest.main()
