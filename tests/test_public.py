import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fake_public  # noqa: E402
from vtcseamless.public import APIError, PublicAPI  # noqa: E402


@pytest.fixture(scope="module")
def api_url():
    srv, url = fake_public.start()
    fake_public.FakePublic.reports.clear()
    fake_public.FakePublic.rate_limit_once = True
    yield url
    srv.shutdown()


def test_reads_and_etag(api_url):
    api = PublicAPI(api_url, retry_s=5)
    assert api.ne()["frozen"]["algo"] == "ne-cells-2"
    m1 = api.ne_manifest()
    m2 = api.ne_manifest()                      # 304 → served from the ETag memo
    assert m1 == m2 and fake_public.FakePublic.manifest_hits == 2
    assert api.ne_manifest(since="2026-10-06T00:00:00Z")["delta"] is True
    assert api.kg("99901")["kg_name"] == "Test" and api.kg("1")is None
    assert api.ne_cells(lon=15.11, lat=47.12).data["ids"]


def test_report_token_and_rate_limit(api_url):
    rep = {"kg": "99901", "algo": "ne-cells-2", "epoch": "2026-10", "source": "bevdirect@v0.3.0", "observer": "t",
           "digest": "0" * 16, "chunks": {"8a1e108cc927fff": "1" * 16}}
    with pytest.raises(APIError):
        PublicAPI(api_url, token="", retry_s=5).report(rep)          # no token → refuses locally
    with pytest.raises(APIError) as e:
        PublicAPI(api_url, token="wrong", retry_s=5).report(rep)     # 404 from the server
    assert e.value.status == 404
    api = PublicAPI(api_url, token=fake_public.TOKEN, retry_s=10)
    r = api.report(rep)                                             # first call is 429 + Retry-After, then 200
    assert r.status == 200 and r.baseline == "this_report" and not r.compared
    r2 = api.report(rep)
    assert r2.compared and r2.chunks_same == 1 and "same=1" in r2.summary()
    assert api.reports("99901").data["reports"]
