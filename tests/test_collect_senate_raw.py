"""
Tests af ingestion/collect_senate_raw.py. Ingen netværkskald: en falsk session spiller eFD.

VIGTIGT: Al eksempel-HTML og -JSON herunder er KONSTRUERET ud fra den dokumenterede struktur
(Django-formular med csrfmiddlewaretoken, JSON med recordsTotal/recordsFiltered/data, links af
formen /search/view/ptr|paper/<uuid>/). Intet er fanget fra det rigtige eFD-site. Testene
beviser derfor kun, at koden gør det, vi tror sitet forventer; --probe skal bekræfte resten.

Kør:  python3 tests/test_collect_senate_raw.py      (eller:  pytest tests/test_collect_senate_raw.py)
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
import collect_senate_raw as c  # noqa: E402
import requests  # noqa: E402

U1 = "aaaaaaaa-1111-2222-3333-444444444444"   # elektronisk
U2 = "bbbbbbbb-1111-2222-3333-444444444444"   # elektronisk, rettelse
U3 = "cccccccc-1111-2222-3333-444444444444"   # papir

GIF = b"GIF89a\x01\x00\x01\x00fake"

HOME_HTML = ('<html><form method="post" action="/search/home/">'
             '<input type="hidden" name="csrfmiddlewaretoken" value="tok123">'
             '<input type="checkbox" name="prohibition_agreement" value="1"></form></html>')
SEARCH_HTML = '<html><form><input type="hidden" name="csrfmiddlewaretoken" value="tok456"></form>Search</html>'
PTR_HTML = "<html><table><tr><td>Ticker</td><td>Transaction Date</td></tr><tr><td>AAPL</td></tr></table></html>"
PAPER_HTML = ('<html><img src="/static/img/logo.png"><img src="/media/paper/abc/page1.gif">'
              '<img src="/media/paper/abc/page2.gif"></html>')


def row(first, last, uuid, kind="ptr", title="Periodic Transaction Report for 03/04/2024", date="03/05/2024"):
    link = f'<a href="/search/view/{kind}/{uuid}/" target="_blank">{title}</a>'
    return [first, last, f"{last}, {first} (Senator)", link, date]


class FakeResp:
    def __init__(self, status=200, body=b"", url="", ctype="text/html", headers=None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.url = url
        self.headers = {"Content-Type": ctype, **(headers or {})}

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")


class FakeSite:
    """Falsk eFD. Registrerer alle kald i self.calls = [(metode, url, data)]."""

    def __init__(self, rows_by_year=None):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("csrftoken", "cookietok")
        self.calls = []
        self.agreed = False
        self.rows_by_year = rows_by_year or {}
        self.pages = {}            # url -> (status, body, ctype)
        self.expire_next_get = False
        self.agree_count = 0

    def request(self, method, url, data=None, headers=None, timeout=None, **kw):
        self.calls.append((method, url, data))
        path = url.replace(c.BASE, "")
        if method == "GET" and path == c.HOME_PATH:
            return FakeResp(body=HOME_HTML, url=url)
        if method == "POST" and path == c.HOME_PATH:
            if (data or {}).get("csrfmiddlewaretoken") != "tok123" or data.get("prohibition_agreement") != "1":
                return FakeResp(403, "CSRF", url=url)
            if not (headers or {}).get("Referer"):
                return FakeResp(403, "no referer", url=url)
            self.agreed = True
            self.agree_count += 1
            return FakeResp(body=SEARCH_HTML, url=c.BASE + c.SEARCH_PAGE_PATH)
        if not self.agreed:
            return FakeResp(body=HOME_HTML, url=c.BASE + c.HOME_PATH)
        if method == "POST" and path == c.DATA_PATH:
            assert headers["X-CSRFToken"] == "cookietok" and headers["Referer"].endswith("/search/")
            assert data["report_types"] == "[11]" and data["filer_types"] == "[]"
            year = data["submitted_start_date"][6:10]
            assert data["submitted_end_date"] == f"12/31/{year} 23:59:59"
            rows = self.rows_by_year.get(year, [])
            s, n = int(data["start"]), int(data["length"])
            body = json.dumps({"draw": 1, "recordsTotal": len(rows), "recordsFiltered": len(rows),
                               "data": rows[s:s + n]})
            return FakeResp(body=body, url=url, ctype="application/json")
        if method == "GET":
            if self.expire_next_get and "/search/view/" in path:
                self.expire_next_get = False
                self.agreed = False
                return FakeResp(body=HOME_HTML, url=c.BASE + c.HOME_PATH)
            status, body, ctype = self.pages.get(path, (404, b"nej", "text/html"))
            return FakeResp(status, body, url=url, ctype=ctype)
        return FakeResp(404, b"?", url=url)

    def posts_to_home(self):
        return [x for x in self.calls if x[0] == "POST" and x[1].endswith(c.HOME_PATH)]


class Base(unittest.TestCase):
    def setUp(self):
        self.old = (c.PAUSE_SECONDS, c.BACKOFF_SECONDS, c.LONG_WAIT_SECONDS, c.PAGE_SIZE)
        c.PAUSE_SECONDS = c.BACKOFF_SECONDS = c.LONG_WAIT_SECONDS = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = c.Paths(self.root)
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        sys.stdout = self._stdout
        c.PAUSE_SECONDS, c.BACKOFF_SECONDS, c.LONG_WAIT_SECONDS, c.PAGE_SIZE = self.old
        self.tmp.cleanup()

    def out(self):
        return sys.stdout.getvalue()

    def site2024(self):
        site = FakeSite({"2024": [row("Tommy", "Tuberville", U1),
                                  row("Jane", "Doe", U2, title="Periodic Transaction Report (Amendment 1) for 03/04/2024",
                                      date="04/01/2024"),
                                  row("Old", "Timer", U3, kind="paper", title="Periodic Transaction Report for 05/05/2024",
                                      date="05/06/2024")]})
        site.pages[f"/search/view/ptr/{U1}/"] = (200, PTR_HTML, "text/html")
        site.pages[f"/search/view/ptr/{U2}/"] = (200, PTR_HTML, "text/html")
        site.pages[f"/search/view/paper/{U3}/"] = (200, PAPER_HTML, "text/html")
        site.pages["/media/paper/abc/page1.gif"] = (200, GIF, "image/gif")
        site.pages["/media/paper/abc/page2.gif"] = (200, GIF, "image/gif")
        return site

    def run_(self, site, years=(2024,), limit=None, answer="ja"):
        return c.run(list(years), limit, root=self.root, session=site, input_fn=lambda prompt="": answer)

    def index_rows(self):
        with self.paths.index.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))


class TestParsing(unittest.TestCase):
    def test_row_electronic(self):
        r = c.parse_row(row("Tommy", "Tuberville", U1))
        self.assertEqual((r["uuid"], r["kind"], r["is_amendment"]), (U1, "ptr", "false"))
        self.assertEqual(r["title"], "Periodic Transaction Report for 03/04/2024")
        self.assertEqual(r["filing_date"], "2024-03-05")
        self.assertEqual((r["first"], r["last"]), ("Tommy", "Tuberville"))
        self.assertEqual(r["url"], f"https://efdsearch.senate.gov/search/view/ptr/{U1}/")

    def test_row_paper_and_amendment(self):
        r = c.parse_row(row("A", "B", U3, kind="paper", title="Periodic Transaction Report (Amendment 2) for 01/02/2023",
                            date="01/03/2023"))
        self.assertEqual((r["kind"], r["is_amendment"], r["filing_date"]), ("paper", "true", "2023-01-03"))

    def test_row_entities_and_markup_in_fields(self):
        r = c.parse_row(["<b>Mary&nbsp;Ann</b>", "O&#39;Neil", "x", f'<a href="/search/view/ptr/{U1}/">T &amp; U</a>',
                         "12/31/2023 00:00:00"])
        self.assertEqual((r["first"], r["last"], r["title"]), ("Mary Ann", "O'Neil", "T & U"))
        self.assertEqual(r["filing_date"], "2023-12-31")

    def test_row_bad_shapes_raise(self):
        for bad in (["a", "b"], "streng", ["a", "b", "c", "ingen link", "01/01/2024"],
                    ["a", "b", "c", '<a href="/search/view/annual/x/">t</a>', "01/01/2024"], ["a", "b", "c", None, "d"]):
            with self.assertRaises(c.SiteError):
                c.parse_row(bad)

    def test_iso_date(self):
        self.assertEqual(c.iso_date("03/05/2024"), "2024-03-05")
        self.assertEqual(c.iso_date("3/5/2024"), "2024-03-05")
        self.assertEqual(c.iso_date(""), "")
        self.assertEqual(c.iso_date("2024-03-05"), "")
        self.assertEqual(c.iso_date("13/40/2024"), "")
        self.assertEqual(c.iso_date(None), "")

    def test_search_json_ok_and_total_fallback(self):
        total, rows = c.parse_search_json(json.dumps({"recordsTotal": 7, "recordsFiltered": 2,
                                                      "data": [row("A", "B", U1), row("C", "D", U2)]}))
        self.assertEqual((total, len(rows)), (2, 2))
        total, rows = c.parse_search_json(json.dumps({"recordsTotal": 5, "data": []}))
        self.assertEqual((total, rows), (5, []))

    def test_search_json_wrong_shape_has_snippet_of_500(self):
        for bad in ("<html>" + "x" * 900, json.dumps({"foo": 1}), json.dumps({"data": "x", "recordsTotal": 1}),
                    json.dumps({"data": []})):
            with self.assertRaises(c.SiteError) as cm:
                c.parse_search_json(bad)
            self.assertLessEqual(len(cm.exception.snippet), 500)
            self.assertTrue(cm.exception.snippet)

    def test_csrf_extraction(self):
        self.assertEqual(c.extract_csrf_token(HOME_HTML), "tok123")
        self.assertEqual(c.extract_csrf_token("<input value='abc' type='hidden' name='csrfmiddlewaretoken'>"), "abc")
        self.assertEqual(c.extract_csrf_token(
            '<input name="other" value="no"><input name="csrfmiddlewaretoken" value="yes">'), "yes")
        self.assertIsNone(c.extract_csrf_token("<html>ingen formular</html>"))
        self.assertIsNone(c.extract_csrf_token(""))

    def test_image_links(self):
        urls = c.extract_image_urls(PAPER_HTML + '<img src="/media/paper/abc/page1.gif">'
                                    '<img src="https://evil.example/media/x.gif">',
                                    f"{c.BASE}/search/view/paper/{U3}/")
        self.assertEqual(urls, [f"{c.BASE}/media/paper/abc/page1.gif", f"{c.BASE}/media/paper/abc/page2.gif"])
        self.assertEqual(c.image_ext(urls[0]), ".gif")
        self.assertEqual(c.image_ext(f"{c.BASE}/media/x/y.JPG"), ".jpg")
        self.assertEqual(c.image_ext(f"{c.BASE}/media/x/y"), ".gif")

    def test_looks_like_image(self):
        self.assertTrue(c.looks_like_image(GIF, "image/gif"))
        self.assertTrue(c.looks_like_image(GIF, ""))
        self.assertFalse(c.looks_like_image(b"<html>login</html>", "image/gif"))
        self.assertFalse(c.looks_like_image(b"", "image/gif"))

    def test_parse_years_and_ua(self):
        self.assertEqual(c.parse_years("2016-2018"), [2016, 2017, 2018])
        self.assertEqual(c.parse_years("2024"), [2024])
        self.assertEqual(c.USER_AGENT, "congress-fund-research/0.1")
        self.assertNotIn("@", c.USER_AGENT)


class TestConsent(Base):
    def test_ask_agreement_only_ja(self):
        for ans, want in (("ja", True), (" JA ", True), ("nej", False), ("", False), ("yes", False), ("jaa", False)):
            self.assertEqual(c.ask_agreement(lambda p="", a=ans: a, print_fn=lambda *_: None), want, ans)

    def test_ask_agreement_eof_is_no(self):
        def boom(prompt=""):
            raise EOFError
        self.assertFalse(c.ask_agreement(boom, print_fn=lambda *_: None))

    def test_prohibitions_printed_in_both_languages(self):
        buf = []
        c.ask_agreement(lambda p="": "nej", print_fn=lambda *a: buf.append(" ".join(map(str, a))))
        text = "\n".join(buf)
        self.assertIn(c.AGREEMENT_STATEMENT, text)
        self.assertIn("kreditvurdering", text)
        self.assertIn("credit rating", text)
        self.assertIn("solicitation of money", text)
        self.assertIn("commercial purpose", text)

    def test_nothing_sent_without_ja(self):
        site = self.site2024()
        results, ok = self.run_(site, answer="nej")
        self.assertFalse(ok)
        self.assertEqual(results, {})
        self.assertEqual(site.calls, [])           # ikke engang en GET
        self.assertFalse(site.agreed)
        self.assertIn("Ingen aftale", self.out())

    def test_main_and_probe_refuse_without_ja(self):
        site = self.site2024()
        self.assertEqual(c.main(["2024"], input_fn=lambda p="": "", session=site, root=self.root), 1)
        self.assertEqual(c.main(["--probe"], input_fn=lambda p="": "nej", session=site, root=self.root), 1)
        self.assertEqual(site.calls, [])
        self.assertFalse(self.paths.senate.exists())

    def test_agree_method_refuses_without_consent(self):
        cl = c.Client(self.site2024())
        with self.assertRaises(RuntimeError):
            cl.agree()

    def test_no_bypass_flag(self):
        site = self.site2024()
        for flag in ("--yes", "--agree", "-y", "--ja"):
            self.assertEqual(c.main([flag, "2024"], input_fn=lambda p="": "nej", session=site, root=self.root), 2)
        self.assertEqual(site.calls, [])


class TestFlow(Base):
    def test_full_run_files_and_index(self):
        site = self.site2024()
        results, ok = self.run_(site)
        self.assertTrue(ok)
        self.assertEqual(results[2024], {"rapporter": 3, "elektroniske": 2, "papir": 1,
                                         "hentet": 3, "fandtes": 0, "fejlet": 0})
        self.assertEqual(len(site.posts_to_home()), 1)
        self.assertEqual(self.paths.html("ptr", 2024, U1).read_text(), PTR_HTML)
        self.assertTrue(self.paths.html("paper", 2024, U3).is_file())
        self.assertEqual(self.paths.images_dir(2024, U3).joinpath("page_1.gif").read_bytes(), GIF)
        self.assertTrue(self.paths.images_dir(2024, U3).joinpath("page_2.gif").is_file())
        self.assertEqual(list(self.paths.senate.rglob("*.part")), [])
        rows = {r["uuid"]: r for r in self.index_rows()}
        self.assertEqual(list(next(iter(rows.values())).keys()), c.INDEX_FIELDS)
        self.assertEqual(rows[U1]["filing_date"], "2024-03-05")
        self.assertEqual(rows[U1]["html_path"], f"data/raw/senate/ptr/2024/{U1}.html")
        self.assertEqual((rows[U2]["is_amendment"], rows[U2]["status"]), ("true", "ok"))
        self.assertEqual((rows[U3]["kind"], rows[U3]["n_images"], rows[U3]["status"]), ("paper", "2", "ok"))
        self.assertEqual(rows[U3]["html_path"], f"data/raw/senate/paper/2024/{U3}.html")

    def test_paging(self):
        c.PAGE_SIZE = 2
        site = self.site2024()
        self.run_(site)
        searches = [x for x in site.calls if x[1].endswith(c.DATA_PATH)]
        self.assertEqual([x[2]["start"] for x in searches], ["0", "2"])
        self.assertEqual(len(self.index_rows()), 3)

    def test_resume_skips_existing_and_second_run_downloads_nothing(self):
        site = self.site2024()
        self.run_(site)
        before = len([x for x in site.calls if "/search/view/" in x[1] or "/media/" in x[1]])
        results, ok = self.run_(site)
        after = len([x for x in site.calls if "/search/view/" in x[1] or "/media/" in x[1]])
        self.assertEqual(before, after)
        self.assertEqual(results[2024], {"rapporter": 3, "elektroniske": 2, "papir": 1,
                                         "hentet": 0, "fandtes": 3, "fejlet": 0})
        self.assertEqual(len(self.index_rows()), 3)

    def test_resume_refetches_empty_and_agreement_page_files(self):
        site = self.site2024()
        p1 = self.paths.html("ptr", 2024, U1)
        p1.parent.mkdir(parents=True)
        p1.write_text("")
        p2 = self.paths.html("ptr", 2024, U2)
        p2.write_text(HOME_HTML)
        results, _ = self.run_(site)
        self.assertEqual(p1.read_text(), PTR_HTML)
        self.assertEqual(p2.read_text(), PTR_HTML)
        self.assertEqual(results[2024]["hentet"], 3)

    def test_missing_image_resumes_only_that_image(self):
        site = self.site2024()
        site.pages["/media/paper/abc/page2.gif"] = (404, b"nej", "text/html")
        results, _ = self.run_(site)
        self.assertEqual(results[2024]["fejlet"], 1)
        rows = {r["uuid"]: r for r in self.index_rows()}
        self.assertEqual(rows[U3]["status"], "failed:billede_2_404")
        self.assertEqual(rows[U3]["n_images"], "1")
        site.pages["/media/paper/abc/page2.gif"] = (200, GIF, "image/gif")
        n_page_fetches = len([x for x in site.calls if x[1].endswith("/paper/" + U3 + "/")])
        self.run_(site)
        self.assertEqual(len([x for x in site.calls if x[1].endswith("/paper/" + U3 + "/")]), n_page_fetches)
        rows = {r["uuid"]: r for r in self.index_rows()}
        self.assertEqual((rows[U3]["status"], rows[U3]["n_images"]), ("ok", "2"))

    def test_paper_without_images_is_failed(self):
        site = self.site2024()
        site.pages[f"/search/view/paper/{U3}/"] = (200, "<html>ingen billeder</html>", "text/html")
        self.run_(site)
        rows = {r["uuid"]: r for r in self.index_rows()}
        self.assertEqual(rows[U3]["status"], "failed:ingen_billeder_fundet")

    def test_html_instead_of_image_not_saved(self):
        site = self.site2024()
        site.pages["/media/paper/abc/page1.gif"] = (200, b"<html>login</html>", "text/html")
        self.run_(site)
        self.assertFalse(self.paths.images_dir(2024, U3).joinpath("page_1.gif").exists())
        rows = {r["uuid"]: r for r in self.index_rows()}
        self.assertEqual(rows[U3]["status"], "failed:billede_1_ikke_et_billede")

    def test_failed_report_404_recorded_and_run_continues(self):
        site = self.site2024()
        site.pages[f"/search/view/ptr/{U1}/"] = (404, b"nej", "text/html")
        results, ok = self.run_(site)
        self.assertTrue(ok)
        self.assertEqual((results[2024]["hentet"], results[2024]["fejlet"]), (2, 1))
        rows = {r["uuid"]: r for r in self.index_rows()}
        self.assertEqual(rows[U1]["status"], "failed:404")
        self.assertFalse(self.paths.html("ptr", 2024, U1).exists())

    def test_limit_indexes_all_downloads_first_n_and_keeps_other_years(self):
        site = self.site2024()
        site.rows_by_year["2023"] = [row("X", "Y", "dddddddd-0000", date="02/02/2023")]
        site.pages["/search/view/ptr/dddddddd-0000/"] = (200, PTR_HTML, "text/html")
        self.run_(site, years=(2023, 2024))
        self.run_(site, years=(2024,), limit=1)
        rows = self.index_rows()
        self.assertEqual([(r["year"], r["uuid"]) for r in rows],
                         [("2023", "dddddddd-0000"), ("2024", U1), ("2024", U2), ("2024", U3)])
        # sortering: år, dato, uuid
        site2 = self.site2024()
        tmp2 = tempfile.TemporaryDirectory()
        try:
            c.run([2024], 1, root=Path(tmp2.name), session=site2, input_fn=lambda p="": "ja")
            with c.Paths(Path(tmp2.name)).index.open(newline="", encoding="utf-8") as fh:
                st = {r["uuid"]: r["status"] for r in csv.DictReader(fh)}
        finally:
            tmp2.cleanup()
        self.assertEqual(st, {U1: "ok", U2: "ikke_hentet", U3: "ikke_hentet"})

    def test_index_merge_no_duplicates(self):
        site = self.site2024()
        for _ in range(3):
            self.run_(site)
        keys = [(r["year"], r["uuid"]) for r in self.index_rows()]
        self.assertEqual(len(keys), len(set(keys)))
        c.write_index(self.paths.index, c.read_index(self.paths.index))
        self.assertEqual(len(self.index_rows()), 3)

    def test_session_expiry_reagrees_after_ja(self):
        site = self.site2024()
        site.expire_next_get = True
        results, ok = self.run_(site)
        self.assertTrue(ok)
        self.assertEqual(results[2024]["fejlet"], 0)
        self.assertEqual(site.agree_count, 2)      # første aftale + automatisk fornyelse
        self.assertIn("udløb", self.out())

    def test_expired_session_search_reagrees(self):
        site = self.site2024()
        cl = c.Client(site)
        cl.consented = True
        cl.agree()
        site.agreed = False
        total, rows = cl.search_page(2024, 0)
        self.assertEqual((total, len(rows)), (3, 3))

    def test_expiry_without_consent_never_agrees(self):
        site = self.site2024()
        cl = c.Client(site)          # consented er False
        with self.assertRaises(c.SiteError):
            cl.search_page(2024, 0)
        self.assertEqual(site.posts_to_home(), [])

    def test_wrong_shape_stops_with_danish_error_and_snippet(self):
        site = FakeSite({"2024": []})
        site.rows_by_year["2024"] = [["kun", "to"]]
        results, ok = self.run_(site)
        self.assertFalse(ok)
        self.assertIsNone(results[2024])
        self.assertIn("FEJL", self.out())
        self.assertIn("første 500 tegn", self.out())
        self.assertIn("'kun'", self.out())

    def test_403_stops_run(self):
        site = self.site2024()
        orig = site.request

        def blocked(method, url, **kw):
            if url.endswith(c.DATA_PATH):
                return FakeResp(403, "Forbidden by WAF " * 100, url=url)
            return orig(method, url, **kw)
        site.request = blocked
        results, ok = self.run_(site)
        self.assertFalse(ok)
        self.assertIn("403", self.out())
        self.assertIn("stopper", self.out())

    def test_retry_on_503_then_ok_and_gives_up_after_three(self):
        site = self.site2024()
        orig, state = site.request, {"n": 0}

        def flaky(method, url, **kw):
            if url.endswith(f"/ptr/{U1}/"):
                state["n"] += 1
                if state["n"] < 3:
                    return FakeResp(503, "x", url=url, headers={"Retry-After": "0"})
            return orig(method, url, **kw)
        site.request = flaky
        results, _ = self.run_(site)
        self.assertEqual(state["n"], 3)
        self.assertEqual(results[2024]["fejlet"], 0)

        site2 = self.site2024()
        site2.pages[f"/search/view/ptr/{U1}/"] = (429, b"x", "text/html")
        tmp = tempfile.TemporaryDirectory()
        try:
            res, _ = c.run([2024], None, root=Path(tmp.name), session=site2, input_fn=lambda p="": "ja")
        finally:
            tmp.cleanup()
        self.assertEqual(len([x for x in site2.calls if x[1].endswith(f"/ptr/{U1}/")]), 3)
        self.assertEqual(res[2024]["fejlet"], 1)

    def test_summary_in_danish(self):
        site = self.site2024()
        results, _ = self.run_(site)
        sys.stdout = io.StringIO()
        c.print_summary(results, self.paths)
        text = self.out()
        self.assertIn("Oversigt pr. år", text)
        self.assertIn("Papir", text)
        self.assertIn("Fejlet", text)


class TestProbe(Base):
    def test_probe_prints_and_saves_but_no_index(self):
        site = self.site2024()
        ok = c.probe(2024, self.root, site, lambda p="": "ja")
        self.assertTrue(ok)
        text = self.out()
        self.assertIn("Sitet siger 3 rapporter", text)
        self.assertIn(U3, text)
        self.assertIn("billeder fundet på siden: 2", text)
        self.assertIn("nøgleord fundet", text)
        self.assertFalse(self.paths.index.exists())
        self.assertEqual(len([x for x in site.calls if x[1].endswith(c.DATA_PATH)]), 1)   # kun første side

    def test_probe_limits_to_five_reports(self):
        rows = [row("A", f"N{i}", f"eeeeeeee-{i:04d}") for i in range(8)]
        site = FakeSite({"2024": rows})
        for i in range(8):
            site.pages[f"/search/view/ptr/eeeeeeee-{i:04d}/"] = (200, PTR_HTML, "text/html")
        c.probe(2024, self.root, site, lambda p="": "ja")
        self.assertEqual(len([x for x in site.calls if "/search/view/" in x[1]]), 5)

    def test_main_usage_errors(self):
        for argv in ([], ["abc"], ["2020-2016"], ["--probe", "x"], ["2020", "0"], ["2020", "1", "2"]):
            self.assertEqual(c.main(argv, input_fn=lambda p="": "nej", session=FakeSite(), root=self.root), 2)
        self.assertIn("Brug:", self.out())


if __name__ == "__main__":
    unittest.main(verbosity=2)
