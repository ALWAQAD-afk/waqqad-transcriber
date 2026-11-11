import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456789:TESTBOTTOKENABCdefGhIjkLmNoPqRsTuVwXyZ",
    "WARRAQ_BOT_TOKEN": "456:TEST",
    "WARRAQ_STORAGE_CHAT_ID": "-1001",
    "WARRAQ_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "TELEGRAM_WEBHOOK_SECRET": "secret",
    "WARRAQ_WEBHOOK_SECRET": "secret",
    "COHERE_API_KEYS": "cohere-secret-key-1,cohere-secret-key-2",
    "COHERE_CONCURRENCY": "3",
})

import app
import diagnostics


class DiagnosticLogsTests(unittest.TestCase):
    def setUp(self):
        diagnostics._COLLECTORS.clear()
        patcher = mock.patch.object(app, "COHERE_API_KEYS", ["cohere-key-1", "cohere-key-2"])
        self.addCleanup(patcher.stop)
        patcher.start()

        self.fake_job = {
            "job_id": "test-job",
            "original_filename": "test.mp3",
            "file_size_bytes": 1024,
            "status": "completed",
            "stage": "done",
            "engine": "cohere",
            "telegram_user_id": "12345",
        }
        patcher_job = mock.patch.object(app, "get_job", side_effect=lambda jid: {**self.fake_job, "job_id": jid})
        self.addCleanup(patcher_job.stop)
        patcher_job.start()

        patcher_save = mock.patch.object(app.transcript_store, "save_transcript")
        self.addCleanup(patcher_save.stop)
        patcher_save.start()

        patcher_tg = mock.patch.object(app, "telegram_send_message")
        self.addCleanup(patcher_tg.stop)
        patcher_tg.start()

        patcher_doc = mock.patch.object(app, "send_document_bytes")
        self.addCleanup(patcher_doc.stop)
        patcher_doc.start()

    def tearDown(self):
        diagnostics._COLLECTORS.clear()

    def test_both_logs_created_on_success(self):
        """1. إنشاء النسختين (المختصرة والتفصيلية) للمهمة الناجحة."""
        job_id = "test-job-success-1"
        input_file = Path("/tmp/input_success.mp3")

        parts = [Path("/tmp/part_001.mp3")]

        def fake_transcribe(jid, part, part_index, part_count):
            return "نص التفريغ الناجح", 1

        short_logs = []
        detailed_logs = []

        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts), \
             mock.patch.object(app, "cohere_transcribe_part", side_effect=fake_transcribe), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "send_transcriber_log", side_effect=lambda text: short_logs.append(text)), \
             mock.patch.object(diagnostics, "save_diagnostic_log", side_effect=lambda jid, st, d, text: detailed_logs.append((jid, st, d, text))), \
             mock.patch.object(app, "_archive_transcript"):

            # Initialize job payload
            app.new_job_payload(job_id, "audio.mp3", 1024, "12345", source="web")
            app.process_transcription_job(job_id, input_file)

        # 1. السجل المختصر: تم إنشاؤه وبقي كما هو دون تغيير نسق أو حقول
        self.assertEqual(len(short_logs), 1)
        self.assertIn("📊 تفريغ مكتمل", short_logs[0])
        self.assertIn("✅ نجح", short_logs[0])
        self.assertIn(job_id, short_logs[0])

        # 2. السجل التفصيلي: تم إنشاؤه وربطه بمعرف المهمة
        self.assertEqual(len(detailed_logs), 1)
        saved_jid, status, diag_data, formatted_text = detailed_logs[0]
        self.assertEqual(saved_jid, job_id)
        self.assertEqual(status, "completed")
        self.assertIn("job_info", diag_data)
        self.assertIn("timestamps", diag_data)
        self.assertIn("transcription_engine", diag_data)
        self.assertIn("results_and_cleanup", diag_data)
        self.assertIn("runtime_resources", diag_data)
        self.assertIn("🔍 سجل تشخيصي تفصيلي", formatted_text)

    def test_detailed_log_created_on_failure(self):
        """2. إنشاء سجل تفصيلي للمهمة الفاشلة وتوثيق مرحلة ونوع الخطأ."""
        job_id = "test-job-fail-1"
        input_file = Path("/tmp/input_fail.mp3")

        short_logs = []
        detailed_logs = []

        def failing_transcribe(jid, part, part_index, part_count):
            raise app.LocalProcessingError("FFmpeg decode error: header broken")

        with mock.patch.object(app, "prepare_cohere_parts", return_value=[Path("/tmp/p1.mp3")]), \
             mock.patch.object(app, "cohere_transcribe_part", side_effect=failing_transcribe), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "send_transcriber_log", side_effect=lambda text: short_logs.append(text)), \
             mock.patch.object(diagnostics, "save_diagnostic_log", side_effect=lambda jid, st, d, text: detailed_logs.append((jid, st, d, text))):

            app.new_job_payload(job_id, "broken.mp3", 2048, "12345", source="web")
            app.process_transcription_job(job_id, input_file)

        # 1. السجل المختصر للفشل
        self.assertEqual(len(short_logs), 1)
        self.assertIn("❌ فشل تفريغ", short_logs[0])

        # 2. السجل التفصيلي للفشل
        self.assertEqual(len(detailed_logs), 1)
        saved_jid, status, diag_data, formatted_text = detailed_logs[0]
        self.assertEqual(saved_jid, job_id)
        self.assertEqual(status, "failed")
        self.assertIsNotNone(diag_data.get("failure_details"))
        self.assertEqual(diag_data["failure_details"]["error_type"], "LocalProcessingError")
        self.assertIn("header broken", diag_data["failure_details"]["error_message"])
        self.assertIn("❌ تفاصيل الخطأ", formatted_text)

    def test_recording_individual_part_durations(self):
        """3. تسجيل مدة كل جزء على حدة بدقة."""
        collector = diagnostics.get_collector("job-parts-timing")

        # محاكاة جزأين بأزمنة مختلفة
        collector.record_part_request(part_index=1, duration_seconds=0.150, part_size_bytes=50000, chars_count=120, status="success", key_index=1)
        collector.record_part_request(part_index=2, duration_seconds=0.320, part_size_bytes=80000, chars_count=210, status="success", key_index=2)

        data = collector.to_dict()
        reqs = data["transcription_engine"]["parts_requests"]
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0]["part_index"], 1)
        self.assertEqual(reqs[0]["duration_seconds"], 0.150)
        self.assertEqual(reqs[0]["chars_count"], 120)

        self.assertEqual(reqs[1]["part_index"], 2)
        self.assertEqual(reqs[1]["duration_seconds"], 0.320)
        self.assertEqual(reqs[1]["chars_count"], 210)

    def test_distinguish_parallel_vs_sequential_execution(self):
        """4. التمييز الصحيح بين التنفيذ المتوازي والتتابعي."""
        # حالة 1: تتابعي عند جزء واحد أو COHERE_CONCURRENCY=1
        parts_single = [Path("/tmp/part_001.mp3")]
        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts_single), \
             mock.patch.object(app, "cohere_transcribe_part", return_value=("نص", 1)), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "finalize_transcript"):
            app.process_transcription_job("job-seq", Path("/tmp/in.mp3"))

        col_seq = diagnostics.get_collector("job-seq")
        self.assertEqual(col_seq.execution_mode, "sequential")
        self.assertEqual(col_seq.actual_concurrency, 1)

        # حالة 2: متوازي عند تعدد الأجزاء والتزامن > 1
        parts_multi = [Path("/tmp/p1.mp3"), Path("/tmp/p2.mp3"), Path("/tmp/p3.mp3")]
        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts_multi), \
             mock.patch.object(app, "cohere_transcribe_part", return_value=("نص", 1)), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "finalize_transcript"), \
             mock.patch.object(app, "COHERE_CONCURRENCY", 3):
            app.process_transcription_job("job-par", Path("/tmp/in.mp3"))

        col_par = diagnostics.get_collector("job-par")
        self.assertEqual(col_par.execution_mode, "parallel")
        self.assertEqual(col_par.actual_concurrency, 3)

    def test_parts_order_verification(self):
        """5. الحفاظ على ترتيب الأجزاء وتأكيد ذلك في السجل التشخيصي."""
        parts = [Path("/tmp/p1.mp3"), Path("/tmp/p2.mp3")]

        def fake_out_of_order(jid, part, part_index, part_count):
            if part_index == 1:
                time.sleep(0.04)
                return "الجزء الأول", 1
            else:
                time.sleep(0.01)
                return "الجزء الثاني", 2

        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts), \
             mock.patch.object(app, "cohere_transcribe_part", side_effect=fake_out_of_order), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "finalize_transcript"):
            app.process_transcription_job("job-order", Path("/tmp/in.mp3"))

        col = diagnostics.get_collector("job-order")
        d = col.to_dict()
        self.assertTrue(d["results_and_cleanup"]["parts_order_verified"])

    def test_no_secret_leakage(self):
        """6. عدم تسريب الأسرار (المفاتيح، الرموز، روابط التوقيع، المحتوى الكامل)."""
        collector = diagnostics.get_collector("job-secrets")

        # محاكاة تسجيل خطأ أو حقول تحوي أسراراً وروابط موقعة
        secret_key = "sk-live-1234567890abcdef"
        bot_token = "123456789:ABCdefGhIjkLmNoPqRsTuVwXyZ"
        signed_url = "https://storage.googleapis.com/audio.mp3?X-Goog-Signature=abcdef123456"

        collector.record_failure(
            stage="transcribing_cohere",
            error=f"Error connecting with Bearer {secret_key} and token={secret_key}",
            error_detail=f"Failed url={signed_url} bot={bot_token}"
        )

        data = collector.to_dict()
        report_text = collector.format_report()

        # تحقق من إخفاء الأسرار في القاموس والنص المنسق
        self.assertNotIn(secret_key, json.dumps(data))
        self.assertNotIn(secret_key, report_text)
        self.assertNotIn(bot_token, report_text)
        self.assertNotIn("X-Goog-Signature", report_text)
        self.assertIn("[REDACTED]", report_text)

        # التحقق من عدم تخزين محتوى التفريغ الكامل في السجل التشخيصي
        self.assertNotIn("transcript_text", data["results_and_cleanup"])
        self.assertIn("final_chars_count", data["results_and_cleanup"])
        self.assertIn("final_words_count", data["results_and_cleanup"])

    def test_transcription_unaffected_if_diagnostic_save_fails(self):
        """7. عدم تأثر نجاح التفريغ إذا فشل حفظ السجل التفصيلي."""
        job_id = "job-fail-safe"
        input_file = Path("/tmp/input_failsafe.mp3")

        def broken_save(jid, st, d, text):
            raise RuntimeError("Database connection crashed during diagnostic save")

        with mock.patch.object(app, "prepare_cohere_parts", return_value=[Path("/tmp/p.mp3")]), \
             mock.patch.object(app, "cohere_transcribe_part", return_value=("النص ممتاز", 1)), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "send_transcriber_log"), \
             mock.patch.object(diagnostics, "save_diagnostic_log", side_effect=broken_save), \
             mock.patch.object(app, "_archive_transcript"):

            # يجب أن تنتهي المهمة بنجاح دون إلقاء أي استثناء
            try:
                app.process_transcription_job(job_id, input_file)
            except Exception as e:
                self.fail(f"process_transcription_job failed due to diagnostic error: {e}")

    def test_get_diagnostic_log_retrieval(self):
        """التحقق من استرجاع السجل التشخيصي من الذاكرة أو إرجاع None للمهام غير الموجودة."""
        job_id = "job-retrieval-test"
        collector = diagnostics.get_collector(job_id)
        collector.record_success(engine="cohere")

        log_data = diagnostics.get_diagnostic_log(job_id)
        self.assertIsNotNone(log_data)
        self.assertEqual(log_data["job_id"], job_id)
        self.assertEqual(log_data["status"], "completed")

        missing = diagnostics.get_diagnostic_log("job-missing-nonexistent")
        self.assertIsNone(missing)

    def test_endpoint_diagnostic_log_access(self):
        """فحص مسار API للحصول على السجل التشخيصي للمستخدم المصرح له."""
        job_id = "job-endpoint-test"
        collector = diagnostics.get_collector(job_id)
        collector.record_success(engine="cohere")

        with app.app.test_client() as client:
            with mock.patch.object(app, "_current_telegram_user", return_value={"id": 12345}), \
                 mock.patch.object(app, "_job_belongs_to", return_value=True):
                res = client.get(f"/diagnostic-log/{job_id}")
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertEqual(data["job_id"], job_id)
                self.assertEqual(data["status"], "completed")


if __name__ == "__main__":
    unittest.main()

