import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from olcr_api.db import Database


class MedicationRecordsTests(unittest.TestCase):
    def setUp(self):
        from olcr_api.medication_records import MedicationRecords
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name)/'test.db')); self.db.initialize()
        self.store = MedicationRecords(self.db)
        self.now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)

    def drug(self, **changes):
        data = dict(name='処方薬A', strength='5mg', dose='1', unit='錠', instructions='1回1錠。24時間で2錠まで。6時間以上あける。',
                    max_per_24h='2', min_interval_hours='6', ingredient='成分A')
        return self.store.register({**data, **changes}, 'rx:'+str(changes))

    def test_exact_drug_identity_includes_strength_and_instructions(self):
        one = self.drug(); two = self.drug(strength='10mg')
        self.assertNotEqual(one['id'], two['id'])
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'): self.store.resolve('処方薬A')
        self.assertEqual(self.store.resolve(one['id'])['strength'], '5mg')
        three = self.drug(instructions='新しい処方：1回1錠')
        self.assertNotEqual(three['id'], one['id'])

    def test_replay_does_not_duplicate_and_changed_payload_is_rejected(self):
        med = self.drug()
        a = self.store.record(med['id'], '1', self.now, 'chat:m1', now=self.now)
        b = self.store.record(med['id'], '1', self.now, 'chat:m1', now=self.now)
        self.assertEqual(a['id'], b['id'])
        with self.assertRaisesRegex(ValueError, 'SOURCE_CONFLICT'):
            self.store.record(med['id'], '2', self.now, 'chat:m1', now=self.now)
        self.assertEqual(self.store.summary(self.now, 'UTC')['medications'][0]['count'], 1)

    def test_limit_interval_and_void_recompute_from_actual_intakes(self):
        med = self.drug()
        self.store.record(med['id'], '1', self.now-timedelta(hours=1), 'one', now=self.now)
        item = self.store.record(med['id'], '2', self.now, 'two', now=self.now)
        codes = {a['code'] for a in self.store.summary(self.now, 'UTC')['alerts']}
        self.assertTrue({'ROLLING_24H_LIMIT', 'INTERVAL', 'SINGLE_DOSE'} <= codes)
        self.store.void(item['id'])
        self.assertEqual(self.store.summary(self.now, 'UTC')['alerts'], [])

    def test_thirty_local_days_excludes_old_and_future_counts_same_day_once(self):
        med = self.drug(max_per_24h=None, min_interval_hours=None)
        for ident, offset in [('old', -31), ('day', -2), ('same', -2), ('now', 0)]:
            self.store.record(med['id'], '1', self.now+timedelta(days=offset), ident, now=self.now)
        report = self.store.summary(self.now, 'Asia/Tokyo')['medications'][0]
        self.assertEqual((report['count'], report['days']), (3, 2))
        with self.assertRaisesRegex(ValueError, 'FUTURE'):
            self.store.record(med['id'], '1', self.now+timedelta(days=1), 'future', now=self.now)

    def test_unknown_limits_do_not_invent_safety_and_units_are_not_converted(self):
        med = self.drug(max_per_24h=None, min_interval_hours=None)
        self.store.record(med['id'], '1', self.now, 'one', now=self.now)
        report = self.store.summary(self.now, 'UTC')
        self.assertEqual(report['medications'][0]['limit_status'], 'unknown')
        with self.assertRaisesRegex(ValueError, 'QUANTITY'):
            self.store.record(med['id'], '-1', self.now, 'invalid', now=self.now)

    def test_same_ingredient_different_products_are_flagged_not_merged(self):
        a = self.drug(); b = self.drug(name='処方薬B')
        self.store.record(a['id'], '1', self.now, 'a', now=self.now)
        self.store.record(b['id'], '1', self.now, 'b', now=self.now)
        report = self.store.summary(self.now, 'UTC')
        self.assertEqual(len(report['medications']), 2)
        self.assertIn('SAME_INGREDIENT', {a['code'] for a in report['alerts']})

    def test_frequency_warning_uses_confirmed_ingredient_and_purpose_not_brand_guess(self):
        med=self.drug(name='商品名A',ingredient='アセトアミノフェン',purpose='頭痛時',max_per_24h=None,min_interval_hours=None)
        for day in range(15):
            self.store.record(med['id'],'1',self.now-timedelta(days=day),f'day:{day}',now=self.now)
        report=self.store.summary(self.now,'UTC')
        self.assertIn('FREQUENT_HEADACHE_USE',{a['code'] for a in report['alerts']})
        other=self.drug(name='商品名B',ingredient='不明な成分',purpose='頭痛時',max_per_24h=None,min_interval_hours=None)
        for day in range(16):
            self.store.record(other['id'],'1',self.now-timedelta(days=day),f'other:{day}',now=self.now)
        report=self.store.summary(self.now,'UTC')
        self.assertEqual(len([a for a in report['alerts'] if a['code']=='FREQUENT_HEADACHE_USE']),1)

    def test_prescription_change_does_not_split_physical_drug_rolling_limit(self):
        old=self.drug(instructions='旧処方',min_interval_hours=None)
        new=self.drug(instructions='新処方',min_interval_hours=None)
        self.store.record(old['id'],'1',self.now-timedelta(hours=1),'old',now=self.now)
        self.store.record(new['id'],'2',self.now,'new',now=self.now)
        self.assertIn('ROLLING_24H_LIMIT',{a['code'] for a in self.store.summary(self.now,'UTC')['alerts']})

    def test_strength_and_unit_case_are_not_silently_converted(self):
        lower=self.drug(strength='1mg'); upper=self.drug(strength='1Mg')
        self.assertNotEqual(lower['id'],upper['id'])
