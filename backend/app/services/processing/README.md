# processing/ — Provider text to documents and traceable chunks

```
RawDocument (source metadata + text, maybe none)
   │  clean_text          remove markup, decode entities, NFC, whitespace
   │  strip_boilerplate   whole boilerplate lines, "Abstract:" label, trailing ©
   │  chunk_spans         overlapping (start, end) spans of the cleaned text
   ▼
ProcessedDocument ── TextChunk[]   each chunk = document.text[start:end], plus its source
```

Pure and synchronous: no network, no database, no model, no randomness. The same input
always gives the same output, ids included.

```python
from app.services.processing import DocumentProcessor, ChunkingConfig

processor = DocumentProcessor()                       # CHUNK_SIZE / CHUNK_OVERLAP
processor = DocumentProcessor(ChunkingConfig(chunk_size=600, chunk_overlap=100))

doc = processor.process_candidate(candidate)          # web or academic search result
docs = processor.process_many(result.candidates)      # empty documents are kept
```

## What v1 accepts

| Input | Text used | Depth | Extractor |
|---|---|---|---|
| `CandidateSource` (Tavily) | the snippet | `snippet` | `tavily_snippet` |
| `AcademicCandidateSource` with an abstract | the abstract | `abstract` | `semantic_scholar_abstract` |
| `AcademicCandidateSource` without one | **nothing** | `metadata` | — |
| `RawDocument` built by hand | whatever is given | as declared | as declared |

**No fetching and no PDF extraction.** The text is what retrieval already returned.

## The rules

1. **Nothing is added.** Cleaning only removes or normalises characters. A test checks
   that the output's characters are a subsequence of the input's. A paper with no
   abstract gets **no text at all**: the title is not substituted, because a title
   says what a paper is about, not what it found, and a claim must not be "supported"
   by one.
2. **A chunk is exactly `document.text[start:end]`.** It is checked by a validator, not
   assumed. Offsets point into the **cleaned** text, which is what becomes
   `Document.text`, so they match what is stored.
3. **Every chunk references its source** by `source_fingerprint` and by the full,
   immutable `SourceMetadata`. A chunk that reaches the vector store on its own can
   still be cited. A source with no DOI, URL or external id is rejected.
4. **Depth describes the text held.** An empty document is `metadata`, with
   `empty_reason` set to `no_text_provided` or `empty_after_cleaning`. It is returned
   rather than dropped, and `to_document_create()` refuses it instead of inventing text
   to satisfy the `document` table.
5. **Stable ids (UUIDv5).** `document_id` comes from the source fingerprint and the
   content hash. `chunk_id` comes from the document id and the span. Re-processing
   gives the same ids, changed text gives new ones, and the ids work as database
   primary keys.

## Chunking strategy

Sizes are **characters**. The defaults are `CHUNK_SIZE=900` and `CHUNK_OVERLAP=150`.

1. A chunk may run to at most `start + chunk_size`.
2. Unless that reaches the end of the text, the end moves **back** to the best boundary
   in the **second half** of the window. The preferred boundaries, in order, are a
   paragraph break (`\n\n`), a sentence end, a line break, and any whitespace. Only
   text with no whitespace at all is cut mid-word.
3. Whitespace is trimmed from both ends of a chunk.
4. The next chunk starts `chunk_overlap` characters before the previous one ended,
   moved **forward** to the start of a word. The real overlap is therefore at most
   `chunk_overlap`, and short of it by less than one word.
5. If the overlap would stop the start from advancing, the next chunk starts with no
   overlap, so the loop always ends.

Searching only the second half of the window bounds chunk size from below. A
boundary near the start would leave a tiny chunk with too little context to be worth
retrieving.

**Why characters and not tokens:** no tokenizer is installed yet (that is
`requirements-ml.txt`), and character offsets are what `document_chunk` stores anyway.
At roughly four characters per token for English, 900 characters is about 225
tokens, well inside any embedding model's window. `token_count` is left empty rather
than estimated.

**At v1's input sizes most documents are one to three chunks.** Snippets are about
1,000 characters and abstracts 1,000 to 1,600. Chunking only starts to matter when
full text arrives.

## Boilerplate removal, and why it is conservative

| Removed | Rule |
|---|---|
| Whole lines such as "Skip to content", "Share on Twitter", "Read more", "Advertisement", cookie banners, newsletter prompts, copyright lines | the **entire line** must match a pattern anchored at its start, and be at most 120 characters |
| A leading `Abstract:` label | only when text follows |
| A trailing copyright notice | needs `©` or "Copyright", plus a four-digit year, at the very end, within 200 characters; never the whole text |

Everything removed is listed in `ProcessedDocument.removed_boilerplate`, so each
decision can be audited. The rule can be switched off with
`ChunkingConfig(strip_boilerplate=False)`.

The anchoring matters. Snippets and abstracts are usually **one line**, so a pattern
of the form "anything, then *all rights reserved*" matches a whole short abstract and
deletes it. That bug existed during development and is now a regression test.

## Limitations

- **Character sizes, not tokens.** The chunk length in tokens varies with language
  and with the amount of numbers and symbols.
- **Sentence detection is a regex.** "e.g." and "Fig. 3" look like sentence ends.
  This only moves a boundary and never loses text, but chunks sometimes end
  mid-thought.
- **Overlap is word-aligned, not sentence-aligned.** A chunk can begin
  mid-sentence. The overlap exists so a sentence cut by one boundary appears whole in
  at least one chunk, provided the sentence is shorter than the overlap.
- **Boilerplate removal is English-only and list-based.** It catches common
  phrases and nothing else: no navigation menus, no related-article lists, no
  structural detection. That is acceptable for snippets, which providers have
  already extracted, but not for raw HTML. The fetch stage will need a real extractor.
- **Tag stripping is a regex.** It knows an inline set (`<i>`, `<sub>`, `jats:*`) that
  it removes silently and treats every other tag as a line break. Text such as
  `a <b and c> d` would be mistaken for a tag.
- **No de-hyphenation**, no mojibake repair, and no language detection. Each of
  these would rewrite text rather than clean it, or needs a dependency.
- **Snippets are fragments.** A Tavily snippet is the provider's extract and can
  start or end mid-sentence. Chunking it does not make it full text, and the depth
  still says `snippet`.
- **Nothing is persisted here.** `to_document_create()`, `spans` (the shape
  `DocumentChunkRepository.add_chunks` takes) and `source.to_source_create()` are the
  bridges. Calling them is the orchestrator's job.
