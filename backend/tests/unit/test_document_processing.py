"""
Document processing: cleaning, boilerplate removal, chunking and the output invariants.

Pure functions over strings, so nothing is mocked and nothing touches a network or a
database. Placeholder sources use `example.invalid` URLs and `10.0000/invalid.*` DOIs,
so nothing here could be mistaken for a real one.

The five required scenarios each have a class: `TestShortDocument`,
`TestLongDocument`, `TestEmptyDocument`, `TestChunkOverlap`,
`TestMetadataPreservation`. The rest pin down the properties those depend on.
"""

import html
import re
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.constants import EvidenceDepth, SourceType
from app.schemas.source import DocumentChunkCreate
from app.services.processing import (
    ChunkingConfig,
    DocumentProcessor,
    ProcessedDocument,
    RawDocument,
    SourceMetadata,
    chunk_spans,
    clean_text,
    content_hash,
    strip_boilerplate,
)
from app.services.retrieval import AcademicCandidateSource, CandidateSource

RETRIEVED_AT = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def web_source(**overrides) -> SourceMetadata:
    values = {
        "source_type": SourceType.WEB,
        "url": "https://example.invalid/article",
        "title": "An Article Under Test",
        "provider": "tavily",
    }
    return SourceMetadata(**{**values, **overrides})


def processor(size: int = 200, overlap: int = 40, **extra) -> DocumentProcessor:
    return DocumentProcessor(ChunkingConfig(chunk_size=size, chunk_overlap=overlap, **extra))


def raw(text: str | None, depth: EvidenceDepth = EvidenceDepth.SNIPPET, **extra) -> RawDocument:
    return RawDocument(source=extra.pop("source", web_source()), text=text, depth=depth, **extra)


def prose(sentences: int, *, start: int = 0) -> str:
    """Numbered sentences, so any chunk can be located in the text by eye."""
    return " ".join(
        f"Sentence {i} reports a modest finding about retrieval quality."
        for i in range(start, start + sentences)
    )


def web_candidate(**overrides) -> CandidateSource:
    result = {
        "url": "https://www.example.invalid/page?utm_source=x",
        "title": "A Page Under Test",
        "content": "A snippet under test. It says one thing.",
        "score": 0.83,
        "published_date": "2026-01-02",
        **overrides,
    }
    candidate = CandidateSource.from_provider_result(result, rank=2, query="test query")
    assert candidate is not None
    return candidate


def academic_candidate(abstract: str | None = "An abstract under test.") -> AcademicCandidateSource:
    return AcademicCandidateSource(
        paper_id="paper-under-test",
        title="A Paper Under Test",
        authors=["Ada Example", "Brian Placeholder"],
        year=2021,
        abstract=abstract,
        url="https://example.invalid/paper",
        doi="10.0000/invalid.one",
        venue="Journal of Tests",
        citation_count=12,
        fingerprint="fp-paper-under-test",
        rank=0,
        evidence_depth=EvidenceDepth.ABSTRACT if abstract else EvidenceDepth.METADATA,
        query="test query",
        retrieved_at=RETRIEVED_AT,
    )


def words(text: str) -> list[str]:
    return text.split()


# --------------------------------------------------------------------------- #
# The five required scenarios
# --------------------------------------------------------------------------- #


class TestShortDocument:
    def test_a_document_shorter_than_one_chunk_is_one_chunk_covering_all_of_it(self):
        doc = processor(size=200).process(raw("A short snippet. Two sentences."))

        assert len(doc.chunks) == 1
        chunk = doc.chunks[0]
        assert chunk.text == doc.text == "A short snippet. Two sentences."
        assert chunk.span == (0, len(doc.text))
        assert chunk.position == 0

    def test_a_document_exactly_one_chunk_long_is_not_split(self):
        text = "x" * 99 + "."
        doc = processor(size=100, overlap=10).process(raw(text))
        assert [c.span for c in doc.chunks] == [(0, 100)]

    def test_a_single_word_is_a_document(self):
        doc = processor().process(raw("Hallucination"))
        assert [c.text for c in doc.chunks] == ["Hallucination"]
        assert doc.depth is EvidenceDepth.SNIPPET
        assert doc.empty_reason is None


class TestLongDocument:
    SIZE, OVERLAP = 300, 60

    @pytest.fixture
    def doc(self) -> ProcessedDocument:
        text = "\n\n".join(prose(12, start=p * 12) for p in range(8))
        return processor(self.SIZE, self.OVERLAP).process(raw(text, EvidenceDepth.FULL_TEXT))

    def test_it_is_split_into_many_chunks_none_larger_than_the_limit(self, doc):
        assert len(doc.text) > 10 * self.SIZE
        assert len(doc.chunks) > 10
        assert all(0 < len(c.text) <= self.SIZE for c in doc.chunks)

    def test_every_chunk_is_exactly_its_span_of_the_document(self, doc):
        for chunk in doc.chunks:
            assert doc.text[chunk.start_char : chunk.end_char] == chunk.text
            # The persistence schema's own span check accepts every chunk.
            DocumentChunkCreate(
                document_id=doc.document_id,
                position=chunk.position,
                text=chunk.text,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
            )

    def test_positions_are_sequential_and_spans_advance(self, doc):
        assert [c.position for c in doc.chunks] == list(range(len(doc.chunks)))
        starts = [c.start_char for c in doc.chunks]
        assert starts == sorted(set(starts))

    def test_no_text_is_lost(self, doc):
        covered = [False] * len(doc.text)
        for chunk in doc.chunks:
            for i in range(chunk.start_char, chunk.end_char):
                covered[i] = True
        missed = [i for i, ch in enumerate(doc.text) if not ch.isspace() and not covered[i]]
        assert missed == []

    def test_chunks_neither_start_nor_end_with_whitespace(self, doc):
        for chunk in doc.chunks:
            assert chunk.text == chunk.text.strip()

    def test_chunks_prefer_to_end_on_a_sentence_or_paragraph(self, doc):
        # The last chunk ends wherever the text does; every other one should land on
        # a boundary, because this text has a sentence end in every half-window.
        for chunk in doc.chunks[:-1]:
            assert chunk.text.endswith("."), chunk.text[-30:]

    def test_text_with_no_whitespace_is_still_chunked_completely(self):
        text = "A" * 1000
        doc = processor(size=100, overlap=20).process(raw(text))

        assert all(len(c.text) <= 100 for c in doc.chunks)
        assert doc.chunks[0].start_char == 0
        assert doc.chunks[-1].end_char == 1000
        for a, b in zip(doc.chunks, doc.chunks[1:], strict=False):
            assert a.end_char - b.start_char == 20  # exact overlap, mid-"word"


class TestEmptyDocument:
    @pytest.mark.parametrize("text", [None, "", "   ", "\n\n\t "])
    def test_no_text_gives_an_empty_document_with_no_chunks(self, text):
        doc = processor().process(raw(text, EvidenceDepth.ABSTRACT))

        assert doc.is_empty
        assert doc.text == ""
        assert doc.chunks == []
        assert doc.empty_reason == "no_text_provided"

    def test_its_depth_is_metadata_whatever_the_input_claimed(self):
        doc = processor().process(raw(None, EvidenceDepth.ABSTRACT))
        assert doc.depth is EvidenceDepth.METADATA
        assert doc.extractor is None

    def test_text_that_cleans_to_nothing_is_reported_as_such(self):
        doc = processor().process(raw("<div>​</div>\n<p>Skip to content</p>"))

        assert doc.is_empty
        assert doc.empty_reason == "empty_after_cleaning"
        assert doc.raw_char_length > 0
        assert doc.removed_boilerplate == ["Skip to content"]

    def test_a_paper_without_an_abstract_is_not_given_one(self):
        """Missing abstract: the title must not be substituted for it."""
        doc = processor().process_candidate(academic_candidate(abstract=None))

        assert doc.text == ""
        assert doc.chunks == []
        assert doc.depth is EvidenceDepth.METADATA
        # The paper is still a recordable source.
        assert doc.source.title == "A Paper Under Test"
        assert doc.source.to_source_create().doi == "10.0000/invalid.one"

    def test_an_empty_document_refuses_to_become_a_document_row(self):
        doc = processor().process(raw(None))
        with pytest.raises(ValueError, match="record the source only"):
            doc.to_document_create(uuid.uuid4())

    def test_empty_documents_are_returned_not_dropped_from_a_batch(self):
        docs = processor().process_many(
            [academic_candidate(abstract=None), academic_candidate(), web_candidate()]
        )
        assert [d.is_empty for d in docs] == [True, False, False]


class TestChunkOverlap:
    @pytest.mark.parametrize("size,overlap", [(120, 30), (200, 0), (200, 99), (500, 150)])
    def test_consecutive_chunks_share_at_most_the_configured_overlap(self, size, overlap):
        doc = processor(size, overlap).process(raw(prose(60)))

        assert len(doc.chunks) > 2
        for a, b in zip(doc.chunks, doc.chunks[1:], strict=False):
            shared = a.end_char - b.start_char
            if overlap == 0:
                assert shared <= 0
                # Only whitespace between them: nothing skipped.
                assert doc.text[a.end_char : b.start_char].strip() == ""
            else:
                assert 0 < shared <= overlap
                # The shared characters really are the end of one and start of the next.
                assert a.text.endswith(doc.text[b.start_char : a.end_char])
                assert b.text.startswith(doc.text[b.start_char : a.end_char])

    def test_overlap_falls_short_of_the_setting_by_less_than_a_word(self):
        doc = processor(200, 60).process(raw(prose(40)))
        longest_word = max(len(w) for w in words(doc.text))
        for a, b in zip(doc.chunks, doc.chunks[1:], strict=False):
            assert a.end_char - b.start_char >= 60 - longest_word - 1

    def test_no_chunk_starts_mid_word(self):
        doc = processor(150, 50).process(raw(prose(30)))
        for chunk in doc.chunks[1:]:
            assert doc.text[chunk.start_char - 1].isspace()

    def test_more_overlap_means_more_chunks(self):
        text = prose(60)
        counts = [len(processor(200, o).process(raw(text)).chunks) for o in (0, 50, 100)]
        assert counts[0] < counts[1] < counts[2]

    @pytest.mark.parametrize("size,overlap", [(100, 100), (100, 150), (100, -1)])
    def test_overlap_must_be_non_negative_and_smaller_than_the_chunk(self, size, overlap):
        with pytest.raises(ValidationError):
            ChunkingConfig(chunk_size=size, chunk_overlap=overlap)

    def test_a_large_overlap_still_terminates_and_covers_everything(self):
        text = prose(30)
        spans = chunk_spans(text, chunk_size=100, chunk_overlap=95)
        assert spans[-1][1] == len(text)
        starts = [s for s, _ in spans]
        assert starts == sorted(set(starts))


class TestMetadataPreservation:
    def test_web_candidate_metadata_reaches_the_document_and_every_chunk(self):
        candidate = web_candidate(content=prose(20))
        doc = processor(200, 40).process_candidate(candidate)

        source = doc.source
        assert source.source_type is SourceType.WEB
        assert source.fingerprint == candidate.fingerprint
        assert source.url == candidate.url
        assert source.canonical_url == candidate.canonical_url
        assert source.title == "A Page Under Test"
        assert source.published_date == "2026-01-02"
        assert source.provider == "tavily"
        assert source.query == "test query"
        assert source.rank == 2
        assert source.relevance_score == pytest.approx(0.83)
        assert source.retrieved_at == candidate.retrieved_at

        assert doc.depth is EvidenceDepth.SNIPPET
        assert doc.extractor == "tavily_snippet"
        assert len(doc.chunks) > 1
        for chunk in doc.chunks:
            assert chunk.source == source
            assert chunk.source_fingerprint == candidate.fingerprint
            assert chunk.document_id == doc.document_id
            assert chunk.depth is EvidenceDepth.SNIPPET

    def test_academic_candidate_metadata_is_preserved_in_full(self):
        candidate = academic_candidate()
        doc = processor().process_candidate(candidate)
        source = doc.source

        assert source.source_type is SourceType.ACADEMIC
        assert source.fingerprint == "fp-paper-under-test"
        assert source.authors == ("Ada Example", "Brian Placeholder")
        assert source.year == 2021
        assert source.doi == "10.0000/invalid.one"
        assert source.external_id == "paper-under-test"
        assert source.venue == "Journal of Tests"
        assert source.citation_count == 12
        assert source.provider == "semantic_scholar"
        assert source.retrieved_at == RETRIEVED_AT
        assert doc.depth is EvidenceDepth.ABSTRACT
        assert doc.extractor == "semantic_scholar_abstract"
        assert doc.chunks[0].source == source

    def test_metadata_survives_serialisation_on_each_chunk(self):
        doc = processor().process_candidate(academic_candidate())
        payload = doc.chunks[0].model_dump(mode="json")
        assert payload["source"]["doi"] == "10.0000/invalid.one"
        assert payload["source"]["authors"] == ["Ada Example", "Brian Placeholder"]
        assert payload["source_fingerprint"] == "fp-paper-under-test"

    def test_metadata_is_immutable(self):
        source = web_source()
        with pytest.raises(ValidationError):
            source.title = "Something else"  # type: ignore[misc]

    def test_round_trips_to_the_persistence_schemas(self):
        doc = processor().process_candidate(academic_candidate())
        source_create = doc.source.to_source_create()
        assert source_create.source_type is SourceType.ACADEMIC
        assert source_create.publication_year == 2021
        assert source_create.authors == ["Ada Example", "Brian Placeholder"]

        document_create = doc.to_document_create(uuid.uuid4())
        assert document_create.text == doc.text
        assert document_create.content_hash == doc.content_hash
        assert document_create.depth is EvidenceDepth.ABSTRACT

    def test_a_source_with_no_identifier_is_rejected(self):
        with pytest.raises(ValidationError, match="DOI, URL or external id"):
            SourceMetadata(source_type=SourceType.WEB, title="Untraceable")

    def test_a_fingerprint_is_derived_when_not_given(self):
        a = web_source(url="https://www.example.invalid/article/")
        b = web_source(url="http://example.invalid/article?utm_source=feed")
        assert a.fingerprint == b.fingerprint  # same identity rule as retrieval

    def test_model_sources_cannot_have_documents(self):
        with pytest.raises(ValidationError):
            SourceMetadata(source_type=SourceType.MODEL, fingerprint="x")


# --------------------------------------------------------------------------- #
# Stable ids
# --------------------------------------------------------------------------- #


class TestStableIds:
    def test_processing_twice_gives_identical_ids(self):
        text = prose(30)
        first = processor().process(raw(text))
        second = processor().process(raw(text))

        assert first.document_id == second.document_id
        assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]

    def test_ids_are_unique_within_a_document(self):
        doc = processor(120, 30).process(raw(prose(40)))
        ids = [c.chunk_id for c in doc.chunks]
        assert len(ids) == len(set(ids))

    def test_changed_text_changes_the_document_id(self):
        a = processor().process(raw("The result was 12 percent."))
        b = processor().process(raw("The result was 21 percent."))
        assert a.document_id != b.document_id

    def test_the_same_text_from_another_source_is_another_document(self):
        text = "Identical syndicated text."
        a = processor().process(raw(text))
        b = processor().process(raw(text, source=web_source(url="https://other.invalid/x")))
        assert a.document_id != b.document_id
        assert a.chunks[0].chunk_id != b.chunks[0].chunk_id

    def test_cosmetic_differences_in_the_input_do_not_change_ids(self):
        a = processor().process(raw("Two  spaces &amp; an entity."))
        b = processor().process(raw("Two spaces & an entity.\n"))
        assert a.document_id == b.document_id

    def test_ids_do_not_depend_on_wall_clock_or_metadata_other_than_identity(self):
        a = processor().process(raw("Same text.", source=web_source(rank=0, query="q1")))
        b = processor().process(raw("Same text.", source=web_source(rank=7, query="q2")))
        assert a.document_id == b.document_id

    def test_ids_are_valid_uuids_usable_as_primary_keys(self):
        doc = processor().process(raw("Some text."))
        assert doc.document_id.version == 5
        assert doc.chunks[0].chunk_id.version == 5


# --------------------------------------------------------------------------- #
# No fabrication
# --------------------------------------------------------------------------- #


class TestNoFabrication:
    SAMPLES = [
        prose(25),
        "<p>Results improved by <b>12%</b> &amp; latency fell.</p><p>Second para.</p>",
        "Abstract: We study H<sub>2</sub>O. © 2021 Example Ltd. All rights reserved.",
        "Line one\r\nLine two​\n\n\n\nLine three",
    ]

    @pytest.mark.parametrize("text", SAMPLES)
    def test_the_output_is_a_deletion_of_the_input(self, text):
        """Cleaning only deletes, so the output's characters appear in the input, in
        order. Word sets would be the wrong test: removing `<sub>` joins `H`, `2`, `O`
        into `H2O`, which is a word the input never spelled out but did contain."""
        doc = processor(60, 15).process(raw(text))
        source = iter(re.sub(r"\s", "", html.unescape(text)))
        output = re.sub(r"\s", "", doc.text)
        assert all(ch in source for ch in output), doc.text

    @pytest.mark.parametrize("text", SAMPLES)
    def test_every_chunk_word_is_a_document_word(self, text):
        doc = processor(60, 15).process(raw(text))
        document_words = set(re.findall(r"\w+", doc.text))
        for chunk in doc.chunks:
            assert set(re.findall(r"\w+", chunk.text)) <= document_words, chunk.text

    @pytest.mark.parametrize("text", SAMPLES)
    def test_the_output_is_never_longer_than_the_input(self, text):
        doc = processor().process(raw(text))
        assert doc.char_length <= doc.raw_char_length

    def test_the_paper_title_is_not_put_into_the_text(self):
        doc = processor().process_candidate(academic_candidate("A short abstract."))
        assert "Paper Under Test" not in doc.text
        assert doc.text == "A short abstract."


# --------------------------------------------------------------------------- #
# Cleaning and boilerplate
# --------------------------------------------------------------------------- #


class TestCleaning:
    def test_whitespace_is_collapsed_and_paragraphs_kept(self):
        assert clean_text("  a  \t b \n\n\n\n c  ") == "a b\n\nc"

    def test_inline_tags_vanish_and_block_tags_break(self):
        assert clean_text("H<sub>2</sub>O") == "H2O"
        # Each block tag is a line break, so a closing and an opening tag together
        # make a paragraph break -- which is what they were.
        assert clean_text("end.</p><p>Next") == "end.\n\nNext"

    def test_entities_are_decoded_after_tags_are_removed(self):
        # An escaped tag in the source is text, and must survive as text.
        assert clean_text("use &lt;b&gt; for bold") == "use <b> for bold"
        assert clean_text("R&amp;D") == "R&D"

    def test_comparisons_are_not_mistaken_for_tags(self):
        assert clean_text("p < 0.05 and x<5") == "p < 0.05 and x<5"

    def test_invisible_and_control_characters_are_removed(self):
        assert clean_text("zero​width soft­hyphen\x00null﻿") == (
            "zerowidth softhyphennull"
        )

    def test_nfc_but_not_nfkc(self):
        assert clean_text("café") == "café"  # composed
        assert clean_text("10 m²") == "10 m²"  # NOT turned into "m2"

    def test_none_and_blank_give_empty_string(self):
        assert clean_text(None) == ""
        assert clean_text("  \n ") == ""


class TestBoilerplate:
    def test_whole_boilerplate_lines_are_removed_and_reported(self):
        text = clean_text(
            "Skip to main content\nThe actual finding.\nShare on Twitter\n"
            "This website uses cookies to improve your experience.\nRead more"
        )
        cleaned, removed = strip_boilerplate(text)
        assert cleaned == "The actual finding."
        assert removed == [
            "Skip to main content",
            "Share on Twitter",
            "This website uses cookies to improve your experience.",
            "Read more",
        ]

    def test_the_same_words_inside_a_sentence_are_kept(self):
        text = "We found that sites which say 'read more' or use cookies retain users longer."
        assert strip_boilerplate(text) == (text, [])

    def test_a_long_line_is_never_removed_even_if_it_matches(self):
        line = "Copyright law " + "and its effect on research reuse " * 6
        cleaned, removed = strip_boilerplate(line.strip())
        assert removed == []
        assert cleaned == line.strip()

    def test_abstract_label_and_trailing_copyright_are_removed(self):
        cleaned, removed = strip_boilerplate(
            "Abstract: We measure X. It rose. © 2021 Elsevier Ltd. All rights reserved."
        )
        assert cleaned == "We measure X. It rose."
        assert removed == ["Abstract:", "© 2021 Elsevier Ltd. All rights reserved."]

    def test_a_short_one_line_abstract_ending_in_a_notice_keeps_its_body(self):
        """Regression: a line rule matching "anything, then 'all rights reserved'"
        deleted this entire abstract, because the whole abstract is one short line."""
        text = "We measure X. It rose sharply. Copyright 2020 Example. All rights reserved."
        cleaned, removed = strip_boilerplate(text)
        assert cleaned == "We measure X. It rose sharply."
        assert removed == ["Copyright 2020 Example. All rights reserved."]

    @pytest.mark.parametrize(
        "line",
        [
            "We use cookies as a proxy for returning users.",
            "Click-through rates rose 4%.",
            "Advertisement spend correlated with recall.",
        ],
    )
    def test_short_content_lines_that_resemble_boilerplate_are_kept(self, line):
        assert strip_boilerplate(line) == (line, [])

    def test_a_real_cookie_banner_is_removed(self):
        banner = "We use cookies to give you the best experience on our site."
        assert strip_boilerplate(f"Body text.\n{banner}") == ("Body text.", [banner])

    def test_a_list_marker_c_is_not_mistaken_for_copyright(self):
        text = "Outcomes were (a) mortality, (b) cost and (c) 2005 readmissions."
        assert strip_boilerplate(text) == (text, [])

    def test_boilerplate_removal_can_be_switched_off(self):
        doc = processor(strip_boilerplate=False).process(raw("Skip to content\nBody."))
        assert doc.text == "Skip to content\nBody."
        assert doc.removed_boilerplate == []


# --------------------------------------------------------------------------- #
# Configuration and invariants
# --------------------------------------------------------------------------- #


class TestConfiguration:
    def test_defaults_come_from_settings(self):
        from app.core.config import get_settings

        config = ChunkingConfig.from_settings()
        assert config.chunk_size == get_settings().CHUNK_SIZE
        assert config.chunk_overlap == get_settings().CHUNK_OVERLAP
        assert DocumentProcessor().config == config

    def test_settings_can_be_overridden(self):
        assert ChunkingConfig.from_settings(chunk_size=400).chunk_size == 400

    def test_the_config_used_is_recorded_on_the_document(self):
        doc = processor(150, 25).process(raw("Text."))
        assert (doc.chunking.chunk_size, doc.chunking.chunk_overlap) == (150, 25)

    def test_chunk_size_is_respected(self):
        text = prose(50)
        for size in (80, 300, 1000):
            doc = processor(size, size // 5).process(raw(text))
            assert max(len(c.text) for c in doc.chunks) <= size


class TestInvariantsAreEnforced:
    def test_a_chunk_whose_span_does_not_match_its_text_is_rejected(self):
        doc = processor().process(raw("Some text here."))
        payload = doc.chunks[0].model_dump()
        payload["end_char"] += 1
        with pytest.raises(ValidationError, match="span"):
            type(doc.chunks[0])(**payload)

    def test_a_document_whose_chunks_do_not_address_its_text_is_rejected(self):
        doc = processor().process(raw("Some text here."))
        payload = doc.model_dump()
        payload["text"] = "Other text here"
        payload["content_hash"] = content_hash("Other text here")
        with pytest.raises(ValidationError, match="not document.text"):
            ProcessedDocument(**payload)

    def test_metadata_depth_with_text_is_rejected_on_input(self):
        with pytest.raises(ValidationError, match="depth is metadata"):
            raw("Some text.", EvidenceDepth.METADATA)

    def test_an_empty_document_cannot_claim_a_text_depth(self):
        doc = processor().process(raw(None))
        payload = doc.model_dump()
        payload["depth"] = EvidenceDepth.ABSTRACT
        with pytest.raises(ValidationError, match="metadata"):
            ProcessedDocument(**payload)
