"""amaranth_sync 단위 테스트. 실행: python3 -m unittest discover -s tests
실서버·실브라우저 테스트는 AMARANTH_TEST_SERVER=gw.example.com 을 지정했을 때만 실행된다."""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "amaranth-calendar"))
import amaranth_sync as a  # noqa: E402

LIVE = os.environ.get("AMARANTH_TEST_SERVER")
PEANUTS = bytes.fromhex("fd621fe5a2b402539dfa147ca9272778")


class DateTest(unittest.TestCase):
    def test_add_days_cases(self):
        self.assertEqual(a.add_days("20261231", 1), "20270101")
        self.assertEqual(a.add_days("20280228", 1), "20280229")
        self.assertEqual(a.add_days("20261019", -14), "20261005")
        self.assertIsNone(a.add_days("2026101", 1))

    def test_server_to_utc_cases(self):
        self.assertEqual(a.server_to_utc("202601151000", 9), "20260115T010000Z")
        self.assertEqual(a.server_to_utc("202611050830", 9), "20261104T233000Z")
        self.assertEqual(a.server_to_utc("202701010100", 9), "20261231T160000Z")
        self.assertEqual(a.server_to_utc("202601151000", 0), "20260115T100000Z")
        self.assertIsNone(a.server_to_utc("bad", 9))

    def test_today_is_ymd(self):
        t = a.today(9)
        self.assertEqual(len(t), 8)
        self.assertTrue(t.isdigit())


class SignTest(unittest.TestCase):
    def test_wehago_sign_vector(self):
        self.assertEqual(
            a.wehago_sign("gcmsExample00001|1234|abc", "0123456789abcdef0123456789abcdef", "1792401619",
                          "/schres/sc111A03", "c2lnbktleS1mb3ItdGVzdA=="),
            "rrTA4XMq7DuWbPc4kGNKqu46R4FSOy3+7usow6gFTY8=")

    def test_pre_login_signature_vector(self):
        self.assertEqual(
            a.pre_login_signature("tok123456789012345678901234567", "20261019",
                                  "abcdef0123456789abcdef0123456789", "/gw/gw050B01"),
            "bL56w0fehHNO1tp4QVkmCIbWYShGKLPeGZVkpcbdxUI=")

    def test_transaction_id_shape(self):
        t = a.transaction_id()
        self.assertEqual(len(t), 32)
        self.assertEqual(t, t.lower())
        int(t, 16)
        self.assertNotEqual(t, a.transaction_id())


class AmaranthTest(unittest.TestCase):
    def test_numeric_ids_accepted(self):
        e = a.Event.from_json({"schSeq": 10001, "mcalSeq": 100, "startDate": "202601151000"})
        self.assertEqual((e.sch_seq, e.mcal_seq), ("10001", "100"))
        self.assertEqual(a.Calendar.from_json({"mcalSeq": 200}).mcal_seq, "200")

    def test_unparsable_lists_are_errors_not_empty_calendars(self):
        bad = [{"schSeq": "../x", "mcalSeq": "100", "startDate": "202601151000"}]
        self.assertRaises(a.Failed, a.parse_events, bad)
        self.assertEqual(a.parse_events([]), [])  # 일정이 정말 없는 기간
        self.assertRaises(a.Failed, a.parse_calendars, [])
        self.assertRaises(a.Failed, a.parse_calendars, [{"mcalSeq": "../x"}])

    def test_company_info_requires_all_fields(self):
        self.assertIsNone(a.company_info_from({"compSeq": "1000", "emailAdd": "user", "emailDomain": "example.com"}, "g"))

    def test_unsafe_ids_rejected(self):
        self.assertTrue(a.is_safe_id("10001"))
        for s in ["", "../x", "a/b"]:
            self.assertFalse(a.is_safe_id(s))
        self.assertIsNone(a.Event.from_json({"schSeq": "../../etc", "mcalSeq": "100", "startDate": "202601151000"}))

    def test_event_from_json_maps_fields(self):
        e = a.Event.from_json({"schSeq": "10001", "mcalSeq": "100", "schTitle": "[대회의실] 주간 회의",
                               "startDate": "202601151000", "endDate": "202601151100", "alldayYn": "N", "schPlace": "",
                               "contents": "안건", "schUserList": "홍길동,김철수", "modifyDate": "202601020304"})
        self.assertEqual((e.sch_seq, e.all_day, e.participants, e.modified), ("10001", False, "홍길동,김철수", "202601020304"))

    def test_cal_list_fixes_empty_type(self):
        c = a.Calendar(mcal_seq="200", cal_type="", title="개인", color="#748FFC")
        self.assertEqual(a.cal_list([c]), [{"mcalSeq": "200", "calType": "E", "adminYn": "Y", "color": "#748FFC"}])

    def test_server_config_flags(self):
        ok = {"groupMap": {"groupSeq": "gcmsExample00001"}, "credentialEncryptUseYn": "N", "samlOnlyYn": "N"}
        enc = {"groupMap": {"groupSeq": "g"}, "credentialEncryptUseYn": "Y", "samlOnlyYn": "N"}
        self.assertEqual(a.ServerConfig.from_json(ok).group_seq, "gcmsExample00001")
        self.assertTrue(a.ServerConfig.from_json(ok).direct_login_supported)
        self.assertFalse(a.ServerConfig.from_json(enc).direct_login_supported)

    def test_company_info_uses_token_group_seq(self):
        uc = {"compSeq": "1000", "groupSeq": "IGNORED", "deptSeq": "500", "emailAdd": "user", "emailDomain": "example.com"}
        self.assertEqual(a.company_info_from(uc, "gcmsExample00001"),
                         {"compSeq": "1000", "groupSeq": "gcmsExample00001", "deptSeq": "500", "emailAddr": "user",
                          "emailDomain": "example.com"})

    def test_events_body_toggle(self):
        b = a.events_body({}, [], "20261005", "20261218", False)
        self.assertEqual((b["mySchYn"], b["startDate"], b["sortDate"]), ("N", "20261005", "Y"))
        self.assertIsNone(b["excelYn"])


def ev(start, end, all_day):
    return a.Event(sch_seq="10001", mcal_seq="100", title="주간 회의, 안건; 공유", start=start, end=end, all_day=all_day,
                   place="대회의실", contents="", participants="홍길동,김철수", modified="202601020304")


class IcsTest(unittest.TestCase):
    def test_timed_event_exact(self):
        self.assertEqual(
            a.to_ics(ev("202601151000", "202601151100", False), "gw.example.com", 9),
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ykoh//amaranth-sync//KO\r\nBEGIN:VEVENT\r\n"
            "UID:amaranth-10001@gw.example.com\r\nDTSTAMP:20260101T180400Z\r\n"
            "DTSTART:20260115T010000Z\r\nDTEND:20260115T020000Z\r\n"
            "SUMMARY:주간 회의\\, 안건\\; 공유\r\nLOCATION:대회의실\r\nDESCRIPTION:참여: 홍길동\\,김철수\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")

    def test_multi_day_all_day(self):
        s = a.to_ics(ev("202602020900", "202602041800", True), "h", 9)
        self.assertIn("DTSTART;VALUE=DATE:20260202\r\n", s)
        self.assertIn("DTEND;VALUE=DATE:20260205\r\n", s)

    def test_end_before_start_uses_start(self):
        s = a.to_ics(ev("202601151000", "", False), "h", 9)
        self.assertIn("DTSTART:20260115T010000Z\r\nDTEND:20260115T010000Z\r\n", s)

    def test_long_korean_line_folds_on_char_boundary(self):
        e = ev("202601151000", "202601151100", False)
        e.title = "가" * 40
        s = a.to_ics(e, "h", 9)
        for line in s.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line)
        self.assertIn("\r\n ", s)

    def test_plain_text_strips_html(self):
        self.assertEqual(a.plain_text("<p>안건&nbsp;1</p><br/>A &amp; B"), "안건 1\n\nA & B")

    def test_file_name_has_date_prefix(self):
        self.assertEqual(a.file_name(ev("202601151000", "", False)), "20260115-10001.ics")


W = ("20261005", "20261218")


def cal(m, t, c):
    return a.Calendar(mcal_seq=m, cal_type="M", title=t, color=c)


def entry(m, f, c):
    return a.Entry(mcal_seq=m, file_name=f, content=c)


def st(only_mine):
    return {"server": "https://x", "only_mine": only_mine}


class VdirTest(unittest.TestCase):
    def setUp(self):
        self.r = Path(tempfile.mkdtemp(prefix="amaranth-vdir-"))
        self.addCleanup(shutil.rmtree, self.r, True)

    def apply(self, state, cals, entries):
        return a.apply_vdir(self.r, state, cals, entries, W)

    def test_writes_entries_and_metadata(self):
        s = self.apply(st(True), [cal("100", "전사 일정", "#748FFC")], [entry("100", "20260115-1.ics", "A")])
        self.assertEqual(s.written, 1)
        self.assertEqual((self.r / "100/displayname").read_text(), "전사 일정")
        self.assertEqual((self.r / "100/color").read_text(), "#748FFC")
        self.assertEqual((self.r / "100/20260115-1.ics").read_text(), "A")

    def test_second_run_is_unchanged(self):
        c, e = [cal("100", "전사 일정", "#748FFC")], [entry("100", "20260115-1.ics", "A")]
        self.apply(st(True), c, e)
        self.assertEqual(self.apply(st(True), c, e), a.Stats(written=0, deleted=0, unchanged=1))

    def test_prunes_only_inside_window(self):
        c, e = [cal("100", "전사 일정", "#748FFC")], [entry("100", "20260115-1.ics", "A")]
        self.apply(st(True), c, e)
        (self.r / "100/20260101-old.ics").write_text("old")
        (self.r / "100/20261106-gone.ics").write_text("gone")
        s = self.apply(st(True), c, e)
        self.assertTrue((self.r / "100/20260101-old.ics").exists())
        self.assertFalse((self.r / "100/20261106-gone.ics").exists())
        self.assertEqual(s.deleted, 1)

    def test_removed_calendar_pruned(self):
        c = [cal("100", "전사 일정", "#748FFC")]
        self.apply(st(True), c + [cal("999", "없어질 캘린더", "#748FFC")], [])
        (self.r / "999/20261110-x.ics").write_text("x")
        self.apply(st(True), c, [])
        self.assertFalse((self.r / "999/20261110-x.ics").exists())

    def test_foreign_files_untouched(self):
        (self.r / "personal").mkdir()
        (self.r / "personal/abc-uid.ics").write_text("x")  # 다른 프로그램의 컬렉션
        (self.r / "personal/20261106-1.ics").write_text("x")  # 이름 형식은 같지만 우리 컬렉션이 아님
        c = [cal("100", "전사 일정", "#748FFC")]
        self.apply(st(True), c, [])  # 첫 실행(상태 파일 없음)
        (self.r / "100/my-notes.ics").write_text("x")  # 우리 컬렉션 안의 다른 파일
        self.apply(st(False), c, [])  # 토글 전환 → 전체 삭제 경로
        for p in ["personal/abc-uid.ics", "personal/20261106-1.ics", "100/my-notes.ics"]:
            self.assertTrue((self.r / p).exists(), p)

    def test_state_change_rebuilds_everything(self):
        c = [cal("100", "전사 일정", "#748FFC")]
        self.apply(st(True), c, [entry("100", "20260115-1.ics", "A")])
        (self.r / "100/20260101-old.ics").write_text("old")
        self.apply(st(False), c, [])
        self.assertFalse((self.r / "100/20260115-1.ics").exists())
        self.assertFalse((self.r / "100/20260101-old.ics").exists())

    def test_invalid_color_not_written(self):
        self.apply(st(True), [cal("100", "전사 일정", "red")], [])
        self.assertFalse((self.r / "100/color").exists())


class GwTest(unittest.TestCase):
    def test_normalize_server_variants(self):
        for s in ["gw.example.com", "gw.example.com/", "https://gw.example.com/#/login", " https://gw.example.com "]:
            self.assertEqual(a.normalize_server(s), ("https://gw.example.com", "gw.example.com"))
        self.assertEqual(a.normalize_server("https://gw.example.com:8443/x"), ("https://gw.example.com:8443", "gw.example.com"))
        self.assertRaises(ValueError, a.normalize_server, "  ")

    def test_form_urlencode_encodes(self):
        self.assertEqual(a.form_urlencode([("a10Domain", "https://x.y"), ("k", "가 b")]),
                         "a10Domain=https%3A%2F%2Fx.y&k=%EA%B0%80%20b")

    def test_creds_fields(self):
        self.assertEqual(a.Creds(auth_token="gcmsExample00001|1234|zz", sign_key="k").group_seq, "gcmsExample00001")

    def test_result_data_codes(self):
        self.assertEqual(a.result_data({"resultCode": 0, "resultData": {"a": 1}}), {"a": 1})
        self.assertIsNone(a.result_data({"resultCode": 200, "resultData": None}))
        self.assertRaises(a.Failed, a.result_data, {"resultCode": 112, "resultMsg": "x"})

    def test_transport_error_is_network(self):
        gw = a.Gw("http://127.0.0.1:1")
        c = a.Creds(auth_token="g|1|t", sign_key="k")
        self.assertRaises(a.NetworkError, gw.post_json, c, "/x", {})
        self.assertRaises(a.NetworkError, gw.pre_login_json, "/x", {})

    @unittest.skipUnless(LIVE, "AMARANTH_TEST_SERVER 미지정")
    def test_live_server_config(self):
        self.assertTrue(a.server_config(a.Gw(LIVE)).group_seq)


class AuthTest(unittest.TestCase):
    def test_secret_lookup_failures_are_rejections(self):
        self.assertRaises(a.AuthRejected, a.secret_lookup, "amaranth-no-such-program", [])
        self.assertRaises(a.AuthRejected, a.secret_lookup, "true", [])  # 빈 stdout

    def test_parse_login_reads_session(self):
        c, secs = a.parse_login({"sessionInfo": {"auth_a_token": "g|1234|t", "hash_key": "hk", "sessionTime": "86400"}})
        self.assertEqual((c.auth_token, c.sign_key, secs), ("g|1234|t", "hk", 86400))
        self.assertIsNone(a.parse_login({"sessionInfo": {}}))

    def test_cache_roundtrip_expiry_and_mode(self):
        d = Path(tempfile.mkdtemp(prefix="amaranth-auth-"))
        self.addCleanup(shutil.rmtree, d, True)
        p = d / "sub/s.json"
        a.save_cache(p, a.Creds(auth_token="g|1|t", sign_key="k"), 1000)
        self.assertEqual(a.load_cache(p, 999).auth_token, "g|1|t")
        self.assertIsNone(a.load_cache(p, 1000))
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_cache_path_sanitizes_id(self):
        self.assertTrue(str(a.cache_path("gw.x.com", "a/b")).endswith("amaranth-sync/gw.x.com-a_b.json"))


class CookiesTest(unittest.TestCase):
    def test_aes128_fips197_vector(self):
        key = bytes(range(16))
        self.assertEqual(a.aes128_decrypt_block(key, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")),
                         bytes.fromhex("00112233445566778899aabbccddeeff"))

    def test_peanuts_key_derivation(self):
        self.assertEqual(a.derive_key(b"peanuts"), PEANUTS)

    def test_decrypt_v10_plain(self):
        self.assertEqual(a.decrypt_chrome(bytes.fromhex("763130c7ce3fb7e49fffc35eef504c9a86925b"), PEANUTS), "cookie-value")

    def test_decrypt_strips_domain_hash(self):
        enc = bytes.fromhex("763130" "7d732018f354868cfa7c9b1d91d21726a59fcdf6a68a01feee0631ca5b6da735"
                            "dca808a69bf20d390dab62bee1f3bf83")
        self.assertEqual(a.decrypt_chrome(enc, PEANUTS), "cookie-value")

    def test_temp_copy_is_private(self):
        d = Path(tempfile.mkdtemp(prefix="amaranth-cookie-src-"))
        self.addCleanup(shutil.rmtree, d, True)
        src = d / "cookies.db"
        src.write_text("cookies")
        Path(f"{src}-wal").write_text("wal")
        src.chmod(0o644)
        with a.private_copy(src) as copy:
            for p in [copy, Path(f"{copy}-wal")]:
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p)
            self.assertEqual(copy.read_text(), "cookies")
        self.assertFalse(copy.exists())

    def test_pct_decode_pipe(self):
        self.assertEqual(a.pct_decode("gcms%7C1234%7Cx"), "gcms|1234|x")

    def test_firefox_default_profile(self):
        ini = "[Profile0]\nName=default\nPath=abc.default\nDefault=1\n\n[Install4F96D1932A9F858E]\nDefault=xyz.default-release\n"
        self.assertEqual(a.default_firefox_profile(ini), "xyz.default-release")
        self.assertEqual(a.default_firefox_profile("[Profile0]\nPath=abc.default\nDefault=1\n"), "abc.default")

    @unittest.skipUnless(LIVE, "AMARANTH_TEST_SERVER 미지정 (이 PC의 Chrome 로그인 세션 필요)")
    def test_live_chrome_creds(self):
        c = a.browser_creds("chrome", None, a.normalize_server(LIVE)[1])
        self.assertTrue(c.group_seq and c.sign_key)


class CliTest(unittest.TestCase):
    def test_parse_password_defaults(self):
        args = a.parse_args("--server gw.x.com --vdir /tmp/v --auth password --login-id user".split())
        self.assertTrue(args.only_mine)
        self.assertEqual((args.past_days, args.future_days, args.utc_offset), (14, 60, 9))

    def test_parse_browser_all(self):
        args = a.parse_args("--server s --vdir /v --all --auth browser --browser firefox --profile p".split())
        self.assertFalse(args.only_mine)

    def test_parse_missing_login_id_errors(self):
        self.assertRaises(a.UsageError, a.parse_args, "--server s --vdir /v --auth password".split())

    def test_parse_expands_home_in_vdir(self):
        args = a.parse_args("--server s --vdir ~/cal --auth browser --browser chrome".split())
        self.assertEqual(args.vdir, Path.home() / "cal")

    def test_repeated_401_after_fresh_login_stops_password_mode(self):
        self.assertIsInstance(a.still_unauthorized(a.PasswordAuth("user"), "https://x", "m"), a.SyncRejected)
        self.assertIsInstance(a.still_unauthorized(a.BrowserAuth("chrome", None), "https://x", "m"), a.SyncTransient)


if __name__ == "__main__":
    unittest.main()
