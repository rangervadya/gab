import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch


class ProgressEstimateTests(unittest.TestCase):
    def test_no_invented_eta_and_only_current_stage_estimate(self):
        from app.import_progress import progress_view
        now=datetime(2026,11,19,16,0,tzinfo=timezone.utc)
        row={'id':1,'status':'processing','progress_stage':'ocr','progress_total':5,
             'progress_pages':[1],'stage_started_at':now-timedelta(seconds=100),
             'processing_started_at':now-timedelta(seconds=200)}
        with patch('app.import_progress.datetime') as clock:
            clock.now.return_value=now
            self.assertIsNone(progress_view(row)['stage_eta_seconds'])
            row['progress_pages']=[1,2]
            result=progress_view(row)
            self.assertEqual(result['stage_eta_seconds'],150)
            self.assertEqual(result['elapsed_seconds'],200)
            row['progress_pages']=[1,2,3,4,5]
            self.assertIsNone(progress_view(row)['stage_eta_seconds'])
