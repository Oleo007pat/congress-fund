"""
Tests af ingestion/collect_house_raw.py. Ingen rigtige netværkskald: en lokal http.server i en tråd
spiller Husets server.

Kør:  python3 tests/test_collect_house_raw.py      (eller:  pytest tests/test_collect_house_raw.py)
"""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestion"))
import collect_house_raw as c  # noqa: E402
import requests  # noqa: E402

PDF = b"%PDF-1.4\nfake pdf\n%%EOF"

XML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
{members}
</FinancialDisclosure>"""


def member(doc_id, ftype="P", first="Nancy", last="Pelosi", prefix="Hon.", suffix="", sd="CA11", date="1/2/2024"):
    return (f"<Member><Prefix>{prefix}</Prefix><Last>{last}</Last><First>{first}</First>"
            f"<Suffix>{suffix}</Suffix><FilingType>{ftype}</FilingType><StateDst>{sd}</StateDst>"
            f"<Year>2024</Year><FilingDate>{date}</FilingDate><DocID>{doc_id}</DocID></Member>")


def make_zip(year, members_xml):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{year}FD.xml", XML_TEMPLATE.format(members=members_xml))
        zf.writestr(f"{year}FD.txt", "ignored")
    return buf.getvalue()


class FakeHouse:
    """Lokal server. routes: sti -> (status, body) eller liste af sådanne (én pr. kald, sidste gentages)."""

    def __init__(self):
        self.routes = {}
        self.hits = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits[self.path] = outer.hits.get(self.path, 0) + 1
                r = outer.routes.get(self.path, (404, b"nej"))
                if isinstance(r, list):
                    r = r[min(outer.hits[self.path], len(r)) - 1]
                status, body = r
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = FakeHouse()
        self.old = (c.BASE, c.PAUSE_SECONDS, c.BACKOFF_SECONDS)
        c.BASE = self.srv.start()
        c.PAUSE_SECONDS = 0
        c.BACKOFF_SECONDS = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = c.Paths(self.root)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = c.USER_AGENT

    def tearDown(self):
        self.srv.stop()
        c.BASE, c.PAUSE_SECONDS, c.BACKOFF_SECONDS = self.old
        self.tmp.cleanup()

    def serve_year(self, year, members_xml, pdfs=None):
        self.srv.routes[f"/financial-pdfs/{year}FD.zip"] = (200, make_zip(year, members_xml))
        for doc_id, resp in (pdfs or {}).items():
            self.srv.routes[f"/ptr-pdfs/{year}/{doc_id}.pdf"] = resp

    def run_(self, years, limit=None):
        return c.run(years, limit, root=self.root, session=self.session)

    def log_rows(self):
        if not self.paths.log.exists():
            return []
        with self.paths.log.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def index_rows(self):
        with self.paths.ptr_index.open(newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))


class TestParsing(unittest.TestCase):
    def test_filters_only_ptr_and_fields(self):
        xml = (member("100") + member("101", ftype="C") + member("102", ftype="A") +
               member("103", first="Mark", last="Green", prefix="", suffix="Jr", sd="TN07", date="12/31/2023"))
        rows = c.parse_index_xml(xml_bytes := XML_TEMPLATE.format(members=xml).encode("utf-8"))
        self.assertEqual([r["doc_id"] for r in rows], ["100", "103"])
        self.assertEqual(rows[1], {"doc_id": "103", "first": "Mark", "last": "Green", "prefix": "",
                                   "suffix": "Jr", "state_dst": "TN07", "filing_date": "2023-12-31"})
        self.assertTrue(xml_bytes)

    def test_duplicate_doc_id_and_empty_doc_id_dropped(self):
        xml = member("100") + member("100") + member("")
        rows = c.parse_index_xml(XML_TEMPLATE.format(members=xml).encode())
        self.assertEqual(len(rows), 1)

    def test_iso_date(self):
        self.assertEqual(c.iso_date("1/2/2024"), "2024-01-02")
        self.assertEqual(c.iso_date("12/31/2023"), "2023-12-31")
        self.assertEqual(c.iso_date(" 3/9/2016 "), "2016-03-09")
        self.assertEqual(c.iso_date(""), "")
        self.assertEqual(c.iso_date("2024-01-02"), "")
        self.assertEqual(c.iso_date("13/40/2024"), "")
        self.assertEqual(c.iso_date(None), "")

    def test_parse_years(self):
        self.assertEqual(c.parse_years("2016-2018"), [2016, 2017, 2018])
        self.assertEqual(c.parse_years("2024"), [2024])

    def test_valid_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pdf"
            self.assertFalse(c.valid_pdf(p))
            p.write_bytes(b"")
            self.assertFalse(c.valid_pdf(p))
            p.write_bytes(b"<html>")
            self.assertFalse(c.valid_pdf(p))
            p.write_bytes(PDF)
            self.assertTrue(c.valid_pdf(p))


class TestDownload(Base):
    def test_basic_download_and_index(self):
        self.serve_year(2024, member("1") + member("2", ftype="C") + member("3", first="Mark", last="Green"),
                        {"1": (200, PDF), "3": (200, PDF)})
        res = self.run_([2024])
        self.assertEqual(res[2024], {"indeks": 2, "hentet": 2, "fandtes": 0, "fejlet": 0})
        self.assertTrue(self.paths.pdf(2024, "1").read_bytes().startswith(b"%PDF"))
        self.assertTrue((self.paths.index_dir / "2024FD.zip").is_file())
        self.assertTrue((self.paths.index_dir / "2024FD.xml").is_file())
        rows = self.index_rows()
        self.assertEqual([r["doc_id"] for r in rows], ["1", "3"])
        self.assertEqual(rows[0]["filing_date"], "2024-01-02")
        self.assertEqual(rows[0]["pdf_path"], "data/raw/ptr/2024/1.pdf")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(list(rows[0].keys()), c.INDEX_FIELDS)
        self.assertEqual(list(self.paths.ptr_dir.rglob("*.part")), [])

    def test_resume_skips_valid_and_refetches_invalid(self):
        self.serve_year(2024, member("1") + member("2") + member("3") + member("4"),
                        {i: (200, PDF) for i in "1234"})
        for i, content in {"1": PDF, "2": b"", "3": b"<html>fejlside</html>"}.items():
            p = self.paths.pdf(2024, i)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        res = self.run_([2024])
        self.assertEqual(res[2024], {"indeks": 4, "hentet": 3, "fandtes": 1, "fejlet": 0})
        self.assertNotIn("/ptr-pdfs/2024/1.pdf", self.srv.hits)       # gyldig fil hentes ikke igen
        for i in "234":
            self.assertEqual(self.srv.hits[f"/ptr-pdfs/2024/{i}.pdf"], 1)
            self.assertEqual(self.paths.pdf(2024, i).read_bytes(), PDF)

    def test_second_run_downloads_nothing(self):
        self.serve_year(2024, member("1") + member("2"), {"1": (200, PDF), "2": (200, PDF)})
        self.run_([2024])
        res = self.run_([2024])
        self.assertEqual(res[2024], {"indeks": 2, "hentet": 0, "fandtes": 2, "fejlet": 0})
        self.assertEqual(self.srv.hits["/ptr-pdfs/2024/1.pdf"], 1)

    def test_404_logged_and_run_continues(self):
        self.serve_year(2024, member("1") + member("2") + member("3"),
                        {"1": (200, PDF), "2": (404, b"nej"), "3": (200, PDF)})
        res = self.run_([2024])
        self.assertEqual(res[2024], {"indeks": 3, "hentet": 2, "fandtes": 0, "fejlet": 1})
        self.assertEqual(self.srv.hits["/ptr-pdfs/2024/2.pdf"], 1)      # 404 forsøges ikke igen
        log = self.log_rows()
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["year"], log[0]["doc_id"], log[0]["http_status_or_error"]), ("2024", "2", "404"))
        self.assertTrue(log[0]["url"].endswith("/ptr-pdfs/2024/2.pdf"))
        self.assertTrue(log[0]["timestamp"])
        status = {r["doc_id"]: r["status"] for r in self.index_rows()}
        self.assertEqual(status, {"1": "ok", "2": "fejlet", "3": "ok"})
        self.assertFalse(self.paths.pdf(2024, "2").exists())

    def test_retry_on_500_then_success(self):
        self.serve_year(2024, member("1"), {"1": [(500, b"x"), (503, b"x"), (200, PDF)]})
        res = self.run_([2024])
        self.assertEqual(res[2024]["hentet"], 1)
        self.assertEqual(res[2024]["fejlet"], 0)
        self.assertEqual(self.srv.hits["/ptr-pdfs/2024/1.pdf"], 3)
        self.assertEqual(self.log_rows(), [])

    def test_retry_on_429_gives_up_after_three_and_logs(self):
        self.serve_year(2024, member("1") + member("2"), {"1": (429, b"x"), "2": (200, PDF)})
        res = self.run_([2024])
        self.assertEqual(self.srv.hits["/ptr-pdfs/2024/1.pdf"], 3)
        self.assertEqual(res[2024], {"indeks": 2, "hentet": 1, "fandtes": 0, "fejlet": 1})
        self.assertEqual(self.log_rows()[0]["http_status_or_error"], "429")

    def test_200_but_not_pdf_is_logged_not_saved(self):
        self.serve_year(2024, member("1"), {"1": (200, b"<html>login</html>")})
        res = self.run_([2024])
        self.assertEqual(res[2024]["fejlet"], 1)
        self.assertFalse(self.paths.pdf(2024, "1").exists())
        self.assertEqual(self.log_rows()[0]["http_status_or_error"], "ikke_pdf")

    def test_missing_year_index_logged_and_next_year_runs(self):
        # 2016 findes ikke på serveren (404), 2017 gør
        self.serve_year(2017, member("9"), {"9": (200, PDF)})
        res = self.run_([2016, 2017])
        self.assertIsNone(res[2016])
        self.assertEqual(res[2017]["hentet"], 1)
        log = self.log_rows()
        self.assertEqual((log[0]["year"], log[0]["doc_id"], log[0]["http_status_or_error"]), ("2016", "INDEX", "404"))

    def test_connection_error_is_logged_with_type(self):
        self.serve_year(2024, member("1"), {"1": (200, PDF)})
        self.srv.stop()                                  # serveren er væk
        self.srv = FakeHouse()                           # (tearDown stopper den nye; start den ikke)
        self.srv.server.server_close()
        self.srv.thread = threading.Thread()             # tearDown kalder shutdown(); undgå hang
        self.srv.server.shutdown = lambda: None
        res = self.run_([2024])
        self.assertIsNone(res[2024])
        self.assertIn("ConnectionError", self.log_rows()[0]["http_status_or_error"])

    def test_limit_downloads_only_first_n_but_indexes_all(self):
        self.serve_year(2024, member("1") + member("2") + member("3"),
                        {i: (200, PDF) for i in "123"})
        res = self.run_([2024], limit=2)
        self.assertEqual(res[2024], {"indeks": 3, "hentet": 2, "fandtes": 0, "fejlet": 0})
        status = {r["doc_id"]: r["status"] for r in self.index_rows()}
        self.assertEqual(status, {"1": "ok", "2": "ok", "3": "ikke_hentet"})

    def test_ptr_index_no_duplicates_on_rerun_and_keeps_other_years(self):
        self.serve_year(2023, member("5"), {"5": (200, PDF)})
        self.serve_year(2024, member("1") + member("2"), {"1": (200, PDF), "2": (200, PDF)})
        self.run_([2023, 2024])
        self.run_([2024])
        self.run_([2024])
        rows = self.index_rows()
        keys = [(r["year"], r["doc_id"]) for r in rows]
        self.assertEqual(keys, [("2023", "5"), ("2024", "1"), ("2024", "2")])
        self.assertEqual(len(keys), len(set(keys)))

    def test_new_filing_appears_on_rerun_and_failed_gets_ok_later(self):
        self.serve_year(2024, member("1") + member("2"), {"1": (200, PDF), "2": (404, b"")})
        self.run_([2024])
        self.serve_year(2024, member("1") + member("2") + member("3"),
                        {"1": (200, PDF), "2": (200, PDF), "3": (200, PDF)})
        res = self.run_([2024])
        self.assertEqual(res[2024], {"indeks": 3, "hentet": 2, "fandtes": 1, "fejlet": 0})
        self.assertEqual([r["status"] for r in self.index_rows()], ["ok", "ok", "ok"])

    def test_user_agent_is_generic(self):
        self.assertEqual(c.USER_AGENT, "congress-fund-research/0.1")
        self.assertNotIn("@", c.USER_AGENT)

    def test_main_usage_errors(self):
        buf = io.StringIO()
        old, sys.stdout = sys.stdout, buf
        try:
            self.assertEqual(c.main([]), 2)
            self.assertEqual(c.main(["abc"]), 2)
            self.assertEqual(c.main(["2020-2016"]), 2)
        finally:
            sys.stdout = old
        self.assertIn("Brug:", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
