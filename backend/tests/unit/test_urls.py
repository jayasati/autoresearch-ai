"""
URL canonicalisation and source identity.

This module is small and heavily tested because two layers depend on agreeing with
it exactly — retrieval deduplicates candidates by it, persistence deduplicates
`source` rows by it. A disagreement would split one page into two sources and
quietly distort every per-source metric.
"""

import pytest

from app.utils.urls import canonicalize_url, source_fingerprint, url_fingerprint


class TestCanonicalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Scheme and host are case-insensitive per RFC 3986.
            ("HTTPS://EXAMPLE.COM/page", "https://example.com/page"),
            # The path is not: most servers treat it case-sensitively.
            ("https://example.com/Page", "https://example.com/Page"),
            # A fragment is client-side; the server never sees it.
            ("https://example.com/page#section", "https://example.com/page"),
            # Default ports are the same address.
            ("https://example.com:443/page", "https://example.com/page"),
            ("http://example.com:80/page", "http://example.com/page"),
            # A non-default port is part of the address.
            ("https://example.com:8443/page", "https://example.com:8443/page"),
            # Trailing slash, except at the root where "/" is the path.
            ("https://example.com/page/", "https://example.com/page"),
            ("https://example.com/", "https://example.com/"),
            ("https://example.com", "https://example.com/"),
            # www is stripped -- the one judgement call here.
            ("https://www.example.com/page", "https://example.com/page"),
        ],
    )
    def test_equivalent_spellings_normalise(self, raw, expected):
        assert canonicalize_url(raw) == expected

    def test_tracking_parameters_are_removed(self):
        assert (
            canonicalize_url("https://example.com/a?utm_source=x&utm_medium=y&fbclid=z")
            == "https://example.com/a"
        )

    def test_meaningful_parameters_are_kept(self):
        """Many sites identify content with a query parameter; dropping them would
        merge two different pages into one source."""
        assert canonicalize_url("https://example.com/view?id=42") == "https://example.com/view?id=42"

    def test_tracking_is_removed_while_meaningful_parameters_survive(self):
        assert (
            canonicalize_url("https://example.com/view?id=42&utm_campaign=spring")
            == "https://example.com/view?id=42"
        )

    def test_parameter_order_does_not_matter(self):
        assert canonicalize_url("https://example.com/a?b=2&a=1") == canonicalize_url(
            "https://example.com/a?a=1&b=2"
        )

    def test_a_malformed_url_is_returned_unchanged_rather_than_raising(self):
        """One bad URL in a page of ten results should cost that result, not the
        whole search."""
        assert canonicalize_url("not a url") == "not a url"
        assert canonicalize_url("/relative/path") == "/relative/path"

    def test_blank_input_yields_blank_output(self):
        assert canonicalize_url("") == ""
        assert canonicalize_url("   ") == ""

    def test_credentials_are_not_part_of_a_documents_identity(self):
        assert canonicalize_url("https://user:pw@example.com/page") == "https://example.com/page"


class TestIdentity:
    def test_http_and_https_are_one_source(self):
        """Counting them separately would inflate source diversity every time a
        search returns both spellings of one page."""
        assert url_fingerprint("http://example.com/a") == url_fingerprint("https://example.com/a")

    def test_the_canonical_url_still_keeps_its_real_scheme(self):
        """Identity ignores the scheme; the URL you would fetch must not."""
        assert canonicalize_url("http://example.com/a") == "http://example.com/a"

    def test_many_spellings_of_one_page_share_one_identity(self):
        spellings = [
            "https://Example.com/page",
            "https://www.example.com/page/",
            "http://example.com:80/page",
            "https://example.com/page?utm_source=z",
            "HTTPS://EXAMPLE.COM/page#top",
        ]
        assert len({url_fingerprint(u) for u in spellings}) == 1

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("https://example.com/a", "https://example.com/b"),
            ("https://a.com/p", "https://b.com/p"),
            ("https://example.com/v?id=1", "https://example.com/v?id=2"),
            ("https://example.com:8443/p", "https://example.com/p"),
        ],
    )
    def test_genuinely_different_pages_stay_different(self, a, b):
        assert url_fingerprint(a) != url_fingerprint(b)


class TestSourceFingerprint:
    def test_a_doi_wins_over_a_url(self):
        """The same paper reachable at three URLs is still one source."""
        assert source_fingerprint(doi="10.1000/x", url="https://a.invalid") == source_fingerprint(
            doi="10.1000/X", url="https://b.invalid"
        )

    def test_a_url_is_used_when_there_is_no_doi(self):
        assert source_fingerprint(url="https://example.com/a") is not None

    def test_an_external_id_is_the_last_resort(self):
        assert source_fingerprint(external_id="S2:abc123") is not None

    def test_no_identifier_yields_no_fingerprint(self):
        """A model assertion with no external reference cannot be deduplicated, and
        giving it a fingerprint would collapse every such assertion into one row."""
        assert source_fingerprint() is None
        assert source_fingerprint(doi="", url="  ", external_id=None) is None

    def test_the_identifier_kind_is_part_of_the_hash(self):
        """So a DOI and a URL that happen to look alike cannot collide."""
        assert source_fingerprint(doi="abc") != source_fingerprint(external_id="abc")

    def test_a_fingerprint_is_a_full_sha256_hex_digest(self):
        value = source_fingerprint(url="https://example.com/a")
        assert value is not None
        assert len(value) == 64
        assert all(c in "0123456789abcdef" for c in value)
