"""
Vector retrieval: indexing, querying, metadata, top-k, and the empty database.

ChromaDB is real — embedded and writing under pytest's tmp_path — because the store's
job *is* its interaction with ChromaDB, and a mocked collection would test the mock.
The embedder is a deterministic bag-of-words fake: no model download, no network, no
torch at import time, and word overlap gives exactly the "closer wording scores
higher" behaviour the ranking tests need. Nothing here measures embedding *quality*;
that is the model's property, not the store's.

One test exists to demonstrate the limitation the README documents: a claim and its
negation score almost identically. **Similarity ranks wording, it does not verify
claims** — any stage reading `similarity` as evidence of truth is wrong, and
`test_contradiction_scores_as_high_as_agreement` is the proof kept in the suite.
"""

import hashlib
import math
import re
import uuid

import pytest

from app.core.config import get_settings
from app.core.constants import EvidenceDepth, SourceType
from app.services.processing import (
    ChunkingConfig,
    DocumentProcessor,
    RawDocument,
    SourceMetadata,
    TextChunk,
)
from app.services.vectorstore import Embedder, IndexResult, RetrievedChunk, VectorStore

EMBEDDING_DIM = 64


class BagOfWordsEmbedder:
    """Deterministic test embedder: tokens hashed into a normalised 64-bin histogram.

    Texts sharing words get similar vectors, so ranking is predictable by eye.
    """

    model_name = "test/bag-of-words-64"

    def _vector(self, text: str) -> list[float]:
        bins = [0.0] * EMBEDDING_DIM
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            bins[int(digest, 16) % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(value * value for value in bins)) or 1.0
        return [value / norm for value in bins]

    def embed_documents(self, texts) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def web_source(url: str = "https://example.invalid/article", **overrides) -> SourceMetadata:
    values = {
        "source_type": SourceType.WEB,
        "url": url,
        "title": "An Article Under Test",
        "provider": "tavily",
        "query": "test query",
        "rank": 1,
        "relevance_score": 0.83,
    }
    return SourceMetadata(**{**values, **overrides})


def academic_source(**overrides) -> SourceMetadata:
    values = {
        "source_type": SourceType.ACADEMIC,
        "doi": "10.0000/invalid.001",
        "external_id": "abc123",
        "title": "A Paper Under Test",
        "provider": "semantic_scholar",
        "authors": ("A. Author", "B. Author"),
        "year": 2024,
        "venue": "Journal of Tests",
        "citation_count": 12,
    }
    return SourceMetadata(**{**values, **overrides})


def chunks_for(text: str, source: SourceMetadata | None = None) -> list[TextChunk]:
    document = DocumentProcessor(ChunkingConfig(chunk_size=200, chunk_overlap=40)).process(
        RawDocument(source=source or web_source(), text=text, depth=EvidenceDepth.SNIPPET)
    )
    assert document.chunks, "test input must produce at least one chunk"
    return document.chunks


def store_in(tmp_path, collection: str = "test_collection") -> VectorStore:
    return VectorStore(
        embedder=BagOfWordsEmbedder(),
        persist_dir=str(tmp_path),
        collection_name=collection,
    )


SENTENCES = {
    "coffee": "Coffee consumption improves working memory in healthy adults.",
    "sleep": "Sleep deprivation impairs cognitive performance on attention tasks.",
    "glaciers": "Alpine glaciers are retreating faster than historical averages.",
    "exercise": "Regular aerobic exercise increases hippocampal volume in older people.",
}


def seeded_store(tmp_path) -> tuple[VectorStore, dict[str, TextChunk]]:
    """One chunk per topic, each from its own source, so ranking tests can name
    the chunk they expect by topic."""
    store = store_in(tmp_path)
    by_topic: dict[str, TextChunk] = {}
    for topic, sentence in SENTENCES.items():
        chunk = chunks_for(sentence, web_source(url=f"https://example.invalid/{topic}"))[0]
        by_topic[topic] = chunk
        store.index_chunks([chunk])
    return store, by_topic


class TestIndexing:
    def test_chunks_are_counted_into_the_collection(self, tmp_path):
        store = store_in(tmp_path)
        chunks = chunks_for("A first sentence about retrieval. " * 20)
        result = store.index_chunks(chunks)

        assert isinstance(result, IndexResult)
        assert result.requested == len(chunks)
        assert result.newly_indexed == len(chunks)
        assert result.already_present == 0
        assert result.duplicates_in_batch == 0
        assert result.collection_count == len(chunks)
        assert store.count() == len(chunks)
        assert result.embedding_model == "test/bag-of-words-64"

    def test_indexing_nothing_is_a_reported_noop(self, tmp_path):
        store = store_in(tmp_path)
        result = store.index_chunks([])
        assert result.requested == 0
        assert result.newly_indexed == 0
        assert result.collection_count == 0

    def test_reindexing_the_same_chunks_adds_nothing(self, tmp_path):
        """Duplicate handling across calls: content-derived ids make the upsert
        idempotent, and the result says so instead of pretending new work happened."""
        store = store_in(tmp_path)
        chunks = chunks_for("The same text indexed twice. " * 20)

        first = store.index_chunks(chunks)
        second = store.index_chunks(chunks)

        assert first.newly_indexed == len(chunks)
        assert second.newly_indexed == 0
        assert second.already_present == len(chunks)
        assert store.count() == len(chunks)

    def test_duplicates_within_one_batch_are_indexed_once(self, tmp_path):
        store = store_in(tmp_path)
        chunks = chunks_for("A text that appears twice in one batch. " * 20)

        result = store.index_chunks(chunks + chunks)

        assert result.requested == 2 * len(chunks)
        assert result.newly_indexed == len(chunks)
        assert result.duplicates_in_batch == len(chunks)
        assert store.count() == len(chunks)

    def test_the_index_persists_across_store_instances(self, tmp_path):
        """Persistent storage: a new store over the same directory sees the data."""
        chunks = chunks_for(SENTENCES["glaciers"])
        store_in(tmp_path).index_chunks(chunks)

        reopened = store_in(tmp_path)
        assert reopened.count() == len(chunks)
        hits = reopened.search("glacier retreat", top_k=1)
        assert hits[0].chunk_id == chunks[0].chunk_id

    def test_a_collection_refuses_a_different_embedding_model(self, tmp_path):
        """Vectors from two models share no space; mixing them must fail loudly at
        open time, not return quiet nonsense at query time."""
        store_in(tmp_path).index_chunks(chunks_for(SENTENCES["coffee"]))

        class OtherEmbedder(BagOfWordsEmbedder):
            model_name = "test/some-other-model"

        with pytest.raises(ValueError, match="some-other-model"):
            VectorStore(
                embedder=OtherEmbedder(),
                persist_dir=str(tmp_path),
                collection_name="test_collection",
            )


class TestQuerying:
    def test_the_closest_wording_ranks_first(self, tmp_path):
        store, by_topic = seeded_store(tmp_path)
        hits = store.search("does coffee consumption improve memory", top_k=4)

        assert hits[0].chunk_id == by_topic["coffee"].chunk_id
        assert [hit.rank for hit in hits] == [0, 1, 2, 3]
        assert all(isinstance(hit, RetrievedChunk) for hit in hits)

    def test_similarity_is_reported_bounded_and_descending(self, tmp_path):
        store, _ = seeded_store(tmp_path)
        hits = store.search("memory and cognition", top_k=4)

        similarities = [hit.similarity for hit in hits]
        assert similarities == sorted(similarities, reverse=True)
        for hit in hits:
            assert -1.0 <= hit.similarity <= 1.0
            assert hit.distance == pytest.approx(1.0 - hit.similarity, abs=1e-6)
            assert hit.query == "memory and cognition"
            assert hit.embedding_model == "test/bag-of-words-64"

    def test_a_blank_query_is_rejected(self, tmp_path):
        store, _ = seeded_store(tmp_path)
        with pytest.raises(ValueError, match="query"):
            store.search("   ")

    def test_contradiction_scores_as_high_as_agreement(self, tmp_path):
        """THE documented limitation, kept executable: similarity measures wording,
        so a claim's negation is a near-perfect 'match'. Nothing downstream may read
        similarity as support for a claim — this test is why."""
        store = store_in(tmp_path)
        claim = "Coffee improves memory in adults."
        negation = "Coffee does not improve memory in adults."
        store.index_chunks(chunks_for(claim, web_source(url="https://example.invalid/pro")))
        store.index_chunks(chunks_for(negation, web_source(url="https://example.invalid/anti")))

        hits = store.search("does coffee improve memory in adults", top_k=2)

        assert len(hits) == 2
        # The negation is not meaningfully further from the query than the claim.
        assert abs(hits[0].similarity - hits[1].similarity) < 0.2
        assert all(hit.similarity > 0.5 for hit in hits)


class TestMetadataPreservation:
    def test_every_source_field_survives_the_round_trip(self, tmp_path):
        store = store_in(tmp_path)
        source = academic_source()
        chunk = chunks_for("An abstract reporting a modest effect. " * 10, source)[0]
        store.index_chunks([chunk])

        hit = store.search("modest effect abstract", top_k=1)[0]

        assert hit.source == source  # every field, not a sample of them
        assert hit.source_fingerprint == chunk.source_fingerprint
        assert hit.depth == chunk.depth

    def test_chunk_identity_and_span_survive(self, tmp_path):
        """Traceability: the retrieved chunk still names its document, its exact
        span of the cleaned text, and the hash to re-verify the text against."""
        store = store_in(tmp_path)
        chunks = chunks_for("Numbered sentences support exact spans. " * 20)
        store.index_chunks(chunks)

        hits = store.search("numbered sentences exact spans", top_k=len(chunks))
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        for hit in hits:
            original = by_id[hit.chunk_id]
            assert hit.document_id == original.document_id
            assert hit.position == original.position
            assert (hit.start_char, hit.end_char) == original.span
            assert hit.text == original.text
            assert hit.content_hash == original.content_hash

    def test_ids_come_back_as_uuids(self, tmp_path):
        store, by_topic = seeded_store(tmp_path)
        hit = store.search("sleep deprivation attention", top_k=1)[0]
        assert isinstance(hit.chunk_id, uuid.UUID)
        assert isinstance(hit.document_id, uuid.UUID)
        assert hit.chunk_id == by_topic["sleep"].chunk_id


class TestTopK:
    def test_top_k_bounds_the_result_count(self, tmp_path):
        store, _ = seeded_store(tmp_path)
        assert len(store.search("memory", top_k=1)) == 1
        assert len(store.search("memory", top_k=3)) == 3

    def test_top_k_defaults_to_the_configured_setting(self, tmp_path):
        store = store_in(tmp_path)
        text = " ".join(
            f"Sentence {i} reports a finding about retrieval quality." for i in range(80)
        )
        store.index_chunks(chunks_for(text))
        assert store.count() > get_settings().TOP_K

        hits = store.search("findings about retrieval quality")
        assert len(hits) == get_settings().TOP_K

    def test_top_k_larger_than_the_collection_returns_everything(self, tmp_path):
        store, _ = seeded_store(tmp_path)
        hits = store.search("anything at all", top_k=50)
        assert len(hits) == store.count() == len(SENTENCES)

    def test_top_k_below_one_is_rejected(self, tmp_path):
        store, _ = seeded_store(tmp_path)
        with pytest.raises(ValueError, match="top_k"):
            store.search("memory", top_k=0)


class TestMMRSearch:
    """Maximal marginal relevance: the optional selection behind EVIDENCE_SELECTION.

    Measured on the stage-19 development pools: near-duplicate snippets from one
    source crowded out passages that establish something (P@4 0.458 plain vs
    0.583 MMR, distinct sources 2.83 vs 3.5, recall unchanged — stage 26).
    """

    def diversified_store(self, tmp_path):
        """One source whose text chunks into near-identical coffee passages, one
        coffee-adjacent chunk from another: plain top-2 takes two of the
        duplicates, MMR should not."""
        store = store_in(tmp_path)
        repetitive = "Coffee consumption improves working memory in healthy adults. " * 8
        repetitive_chunks = chunks_for(
            repetitive.strip(), web_source("https://a.invalid/coffee")
        )
        assert len(repetitive_chunks) >= 2  # the premise: several near-duplicates
        store.index_chunks(
            repetitive_chunks
            + chunks_for(
                "Working memory gains from coffee were smaller in older adults, "
                "a second study of memory and coffee reports.",
                web_source("https://b.invalid/other"),
            )
        )
        return store

    def test_the_first_pick_is_the_plain_best_hit(self, tmp_path):
        store = self.diversified_store(tmp_path)
        plain = store.search("coffee working memory", top_k=1)
        mmr = store.search_mmr("coffee working memory", top_k=1)
        assert mmr[0].chunk_id == plain[0].chunk_id  # relevance dominates pick one

    def test_mmr_breaks_up_near_duplicates(self, tmp_path):
        store = self.diversified_store(tmp_path)
        plain = store.search("coffee working memory", top_k=2)
        mmr = store.search_mmr("coffee working memory", top_k=2)

        plain_sources = {hit.source_fingerprint for hit in plain}
        mmr_sources = {hit.source_fingerprint for hit in mmr}
        assert len(plain_sources) == 1  # the failure mode: two near-duplicates
        assert len(mmr_sources) == 2

    def test_ranks_are_selection_order_and_results_validate(self, tmp_path):
        store = self.diversified_store(tmp_path)
        hits = store.search_mmr("coffee working memory", top_k=3)
        assert [hit.rank for hit in hits] == [0, 1, 2]
        for hit in hits:
            assert isinstance(hit, RetrievedChunk)
            assert 0.0 <= hit.similarity <= 1.0

    def test_empty_collection_and_bad_inputs_behave_like_plain_search(self, tmp_path):
        store = store_in(tmp_path)
        assert store.search_mmr("anything", top_k=3) == []
        with pytest.raises(ValueError, match="top_k"):
            store.search_mmr("x", top_k=0)
        with pytest.raises(ValueError, match="query"):
            store.search_mmr("   ", top_k=2)
        with pytest.raises(ValueError, match="mmr_lambda"):
            store.search_mmr("x", top_k=2, mmr_lambda=1.5)


class TestEmptyDatabase:
    def test_searching_an_empty_collection_returns_no_results(self, tmp_path):
        """Before anything is indexed, 'nothing similar exists' is an answer,
        not an exception."""
        store = store_in(tmp_path)
        assert store.count() == 0
        assert store.search("anything", top_k=5) == []

    def test_an_empty_collection_still_validates_its_queries(self, tmp_path):
        store = store_in(tmp_path)
        with pytest.raises(ValueError, match="query"):
            store.search("")
        with pytest.raises(ValueError, match="top_k"):
            store.search("anything", top_k=-1)


class TestEmbedderContract:
    def test_the_fake_satisfies_the_protocol(self):
        assert isinstance(BagOfWordsEmbedder(), Embedder)

    def test_the_real_embedder_is_lazy(self):
        """Constructing the Sentence Transformers embedder must not load the model
        (and must not import torch) — the app builds one at startup."""
        from app.services.vectorstore.embedder import SentenceTransformerEmbedder

        embedder = SentenceTransformerEmbedder()
        assert embedder.model_name == get_settings().EMBEDDING_MODEL
        assert embedder._model is None
