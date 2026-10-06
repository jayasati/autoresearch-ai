"""
URL canonicalisation and source fingerprinting.

This lives in `utils/` rather than in the retrieval service because **two layers
must agree on it**: retrieval deduplicates candidates by it, and persistence
deduplicates `source` rows by it. If the two normalised differently, the same page
retrieved by two runs would become two rows, and every per-source metric — source
diversity, how often a paper is cited, whether a citation is fabricated — would be
computed over a split identity.

So there is exactly one definition here, and both callers use it.
"""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parameters that identify a *referrer*, not a document. Two URLs differing only in
# these point at the same page, so they must normalise to one.
TRACKING_PARAMETERS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "gclid",
        "gclsrc",
        "dclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "spm",
        "_hsenc",
        "_hsmi",
    }
)

DEFAULT_PORTS = {"http": 80, "https": 443}


def canonicalize_url(url: str) -> str:
    """Reduce a URL to a stable form for comparison.

    What it does, and why each step is safe:

    - **Lower-cases the scheme and host.** Both are case-insensitive by RFC 3986.
      The *path* is left alone, because it is case-sensitive on most servers.
    - **Drops the fragment.** `#section` is a client-side anchor; the server never
      sees it, so it cannot identify a different document.
    - **Removes a default port.** `https://x:443/` and `https://x/` are one address.
    - **Strips tracking parameters** (see above) and **sorts the rest**, so
      `?b=2&a=1` and `?a=1&b=2` agree. Remaining parameters are kept, because many
      sites genuinely identify content with them (`?id=42`, `?page=3`).
    - **Strips a trailing slash**, except on the root path, where `/` is the path.
    - **Strips a leading `www.`** — a deliberate trade-off. Almost every site serves
      the same content at both, and treating them separately would double-count
      sources far more often than merging them wrongly loses one. It is the one step
      here that is a judgement rather than a rule.

    Returns the input unchanged if it cannot be parsed, rather than raising: a
    malformed URL from a search provider should not take down a whole search.
    """
    if not url or not url.strip():
        return ""

    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    if not parts.scheme or not parts.netloc:
        # Not an absolute URL; there is nothing reliable to canonicalise.
        return raw

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port and parts.port != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    # Credentials in a URL are not part of the document's identity.

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def _identity_basis(url: str) -> str:
    """The canonical URL with the http/https distinction removed.

    Deliberately different from `canonicalize_url`, and the split matters:

    - the **canonical URL** keeps its real scheme, because it is what you would
      actually fetch and what a citation should point at;
    - the **identity basis** does not, because `http://x/p` and `https://x/p` are
      the same document in practice. Treating them as two sources would inflate
      source diversity — a number this project reports — every time a search returns
      both spellings.
    """
    canonical = canonicalize_url(url)
    if canonical.startswith("http://"):
        return "https://" + canonical[len("http://") :]
    return canonical


def url_fingerprint(url: str) -> str:
    """SHA-256 identity of a web address. The deduplication key for a web source."""
    return hashlib.sha256(_identity_basis(url).encode("utf-8")).hexdigest()


def source_fingerprint(
    *, doi: str | None = None, url: str | None = None, external_id: str | None = None
) -> str | None:
    """Identity hash for a source, from the strongest identifier available.

    DOI first: it is the most stable identifier a paper has, so the same paper
    reachable at three URLs is still one source. Then the canonical URL, then a
    provider's own id.

    Returns `None` when nothing identifies the source — a model assertion with no
    external reference. That case cannot be deduplicated and must not be given a
    fingerprint, or every such assertion would collapse into one row.
    """
    if doi and doi.strip():
        basis = f"doi:{doi.strip().lower()}"
    elif url and url.strip():
        basis = f"url:{_identity_basis(url)}"
    elif external_id and external_id.strip():
        basis = f"ext:{external_id.strip()}"
    else:
        return None
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()
