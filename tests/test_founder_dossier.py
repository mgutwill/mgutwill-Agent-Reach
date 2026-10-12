import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "founder_dossier", Path(__file__).parent.parent / "scripts" / "founder_dossier.py"
)
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)

FORM_D = """<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/formd">
  <primaryIssuer><entityName>Acme Labs Inc</entityName></primaryIssuer>
  <relatedPersonsList>
    <relatedPersonInfo>
      <relatedPersonName><firstName>Ada</firstName><lastName>Lovelace</lastName></relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>Executive Officer</relationship>
        <relationship>Director</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
  </relatedPersonsList>
  <offeringData>
    <industryGroup><industryGroupType>Technology</industryGroupType></industryGroup>
    <offeringSalesAmounts>
      <totalOfferingAmount>8000000</totalOfferingAmount>
      <totalAmountSold>5500000</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
</edgarSubmission>"""


def test_parse_form_d_extracts_amounts_and_people():
    parsed = fd.parse_form_d(FORM_D)
    assert parsed["issuer"] == "Acme Labs Inc"
    assert parsed["offering"] == 8_000_000
    assert parsed["sold"] == 5_500_000
    assert parsed["industry"] == "Technology"
    assert parsed["people"] == [
        {"name": "Ada Lovelace", "roles": ["Executive Officer", "Director"]}
    ]


@pytest.mark.parametrize(
    "sold,offering,expected",
    [(5_500_000, 8_000_000, True), (None, 25_000_000, False), (None, None, False), (1_000_000, 9_000_000, False)],
)
def test_in_range_prefers_amount_sold(sold, offering, expected):
    assert fd.in_range({"sold": sold, "offering": offering}, 3_000_000, 20_000_000) is expected


@pytest.mark.parametrize("raw", ["https://www.Acme.com/about", "acme.com", "WWW.acme.com"])
def test_clean_domain_normalises(raw):
    assert fd.clean_domain(raw) == "acme.com"


@pytest.mark.parametrize("raw", ["localhost", "acme.com@evil.com", "a b.com", "http://[::1]/x"])
def test_clean_domain_rejects_junk(raw):
    with pytest.raises(ValueError):
        fd.clean_domain(raw)


def test_blog_dates_newest_first():
    md = "Posted 2025-03-01 ... Jan 5, 2026 ... 2025-03-01"
    assert fd.blog_dates(md) == ["Jan 5, 2026", "2025-03-01"]


def test_dossier_has_blank_hook_and_handles_missing_data():
    text = fd.build_dossier("Acme", "acme.com", {"/": None, "/blog": None}, None, None, 3, 20)
    assert "No Form D found" in text
    assert "not fetched" in text
    assert "agent: fill in" in text
