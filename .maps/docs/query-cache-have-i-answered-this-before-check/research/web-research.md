# Web Research: Query Cache — Have I Answered This Before?

## Search Terms Used

- LLM query caching strategies semantic caching exact match hybrid 2024 2025
- GPTCache semantic similarity caching LLM how it works embedding threshold
- LangChain semantic cache embedding similarity LLM caching implementation
- question deduplication slug string matching vs embedding similarity trade-offs NLP
- semantic caching false positive rate cosine similarity threshold tuning LLM production
- LLM cache staleness invalidation strategy knowledge freshness expiry time-to-live
- exact string normalization cache key lowercase strip punctuation fuzzy hash question deduplication
- UX patterns LLM cached answers transparency user notification chatbot design
- GPTCache architecture how it detects similar questions vector similarity search workflow

---

## Key Findings

### 1. LLM Query Caching Strategies: The Three Tiers

Three well-established tiers of LLM caching exist, from simplest to most sophisticated:

**Tier 1 — Exact-match caching (hash-based)**
- Hashes the input string (e.g. SHA-256 after normalization) and stores/retrieves by hash key.
- Sub-millisecond lookup, zero false-positive rate.
- Requires identical (or near-identical after normalization) text to hit.
- Surprisingly effective in practice: roughly **18% of real-world LLM requests are exact or near-exact duplicates** in production systems.
- Best fit: automated pipelines, FAQ bots, batch queries with repeated identical inputs.
- Sources: [Preto.ai semantic caching](https://preto.ai/blog/semantic-caching-llm/), [TianPan.co production caching](https://tianpan.co/blog/2026-04-10-semantic-caching-llm-production)

**Tier 2 — Normalized/fuzzy exact-match caching**
- Normalize input first: lowercase, strip punctuation, collapse whitespace, then hash.
- Catches the same question asked with minor variations ("how does auth work?" vs "How does auth work").
- Still no embedding infrastructure needed — pure string computation.
- Text normalization best practice: lowercase → trim whitespace → strip punctuation → hash (SHA-256 or similar).
- Sources: [PyImageSearch semantic cache safety](https://pyimagesearch.com/2026/05/04/semantic-caching-for-llms-ttls-confidence-and-cache-safety/), [Redis fuzzy matching](https://redis.io/blog/what-is-fuzzy-matching/)

**Tier 3 — Semantic/embedding-based caching**
- Convert question to a dense embedding (e.g. OpenAI `text-embedding-ada-002` or local ONNX model).
- Search a vector index (FAISS, Milvus, Redis Vector, etc.) for approximate nearest neighbors.
- Return cached response if cosine similarity exceeds a configurable threshold (typically 0.85–0.97).
- Handles rephrased questions: "What is your return policy?" ↔ "How do I return an item?".
- Requires embedding model + vector store infrastructure.
- Sources: [GPTCache paper](https://aclanthology.org/2023.nlposs-1.24.pdf), [GPTCache GitHub](https://github.com/zilliztech/GPTCache), [TrueFoundry semantic caching guide](https://www.truefoundry.com/blog/semantic-caching)

**Recommended pattern — Hybrid (Tier 1 + Tier 3 in sequence):**
Most production systems implement both: exact match first (fast, zero false positives), then semantic search on miss. This is the architecture used by tools like GPTCache and LangChain's semantic cache integrations.

---

### 2. How GPTCache and LangChain Detect "Similar Enough" Questions

**GPTCache architecture** (six core components):
1. **Adapter** — integrates with OpenAI/LangChain API
2. **Pre-processor** — normalizes/cleans input query
3. **Embedding generator** — converts query to vector (OpenAI API or local ONNX model)
4. **Cache manager** — stores query+response pairs
5. **Similarity evaluator** — computes cosine similarity between query embedding and stored embeddings; decides hit/miss against threshold
6. **Post-processor** — formats cached response to match API contract

The default cosine similarity threshold in GPTCache is **0.8**, but configurable. The workflow:
- Query → embedding → ANN search (HNSW) → cosine similarity score → threshold comparison → hit/miss.
- Reduces API calls by up to **68.8%** with hit rates between 61.6–68.8% in tested datasets.
- Sources: [GPTCache GitHub](https://github.com/zilliztech/GPTCache), [GPT Semantic Cache paper](https://arxiv.org/html/2411.05276v3), [GPTCache practical guide](https://bhavishyapandit9.substack.com/p/gptcache-a-practical-guide)

**LangChain semantic cache implementations:**
- `RedisSemanticCache`: uses Redis vector similarity search; configurable `distance_threshold`.
- `CassandraSemanticCache`: Cassandra-backed vector store for semantic similarity lookup.
- Both follow the same pattern: embed → similarity search → threshold → hit/miss.
- Sources: [LangChain RedisSemanticCache docs](https://reference.langchain.com/v0.3/python/redis/cache/langchain_redis.cache.RedisSemanticCache.html), [MongoDB LangChain semantic cache](https://www.mongodb.com/docs/atlas/ai-integrations/langchain/memory-semantic-cache/), [CassIO LangChain guide](https://cassio.org/frameworks/langchain/semantic-caching-llm-responses/)

---

### 3. Trade-offs: Slug/String Matching vs. LLM-Based vs. Embedding-Based Similarity

| Approach | Speed | Infrastructure | False Positive Risk | Catch Rate | Best For |
|---|---|---|---|---|---|
| Slug / normalized string match | Sub-ms | None | Near-zero | Exact/near-exact only | Developer tools, scripted pipelines |
| String normalization + hash | Sub-ms | None | Near-zero | Minor variation (case, punctuation) | CLI tools, structured inputs |
| Fuzzy string (Levenshtein/MinHash) | Fast (ms) | None | Low | Typos, abbreviations | Short FAQ queries |
| Embedding cosine similarity | 3–8ms + embedding time | Vector store + embedding model | Medium (tunable) | Paraphrases, synonyms | Conversational chatbots |
| LLM-based similarity validation | 100ms+ overhead | LLM API call | Low after reranking | Near-perfect with reranking | High-precision use cases |

**The "dangerous zone" for embedding-based similarity**: cosine scores between 0.85–0.92. Questions are topically related but may not share an answer. Incorrect hits in this range are hard to prevent without a second-stage classifier or LLM reranker.

**Recommended threshold tiers:**
- **0.85** (aggressive): higher hit rate, higher false positive risk. OK for general FAQs.
- **0.92** (balanced): industry sweet spot for production. Catches clear rephrasings, rejects distinct-but-similar queries.
- **0.94–0.97** (conservative): near-exact required. Approaches string matching efficiency. Use for factual or safety-sensitive domains.

**Starting point**: Always measure your exact duplicate rate before adding semantic complexity. If 15–20% of your queries are exact repeats, Tier 1 alone yields significant value with no infrastructure cost.

- Sources: [TianPan.co production caching](https://tianpan.co/blog/2026-04-10-semantic-caching-llm-production), [Preto.ai semantic caching](https://preto.ai/blog/semantic-caching-llm/), [InfoQ false positives in RAG](https://www.infoq.com/articles/reducing-false-positives-retrieval-augmented-generation/), [Maxim semantic caching](https://www.getmaxim.ai/articles/how-to-optimize-llm-cost-and-latency-with-semantic-caching/)

---

### 4. Staleness, TTL, and Cache Invalidation

**Event-driven invalidation is the preferred primary mechanism.** When underlying source data changes, invalidate affected cache entries proactively rather than relying on TTL alone.

**TTL as a safety net**: Even systems with event-driven invalidation use TTL as a backstop to prevent worst-case staleness. TTL duration should reflect information volatility — definitions/architecture explanations can have long TTLs (days/weeks); dynamic data needs short TTLs (minutes/hours).

**Application-level TTL validation** (vs. relying on Redis/DB expiry) is recommended for correctness:
- Check at read time: `now > created_at + ttl` → treat as miss, remove from cache.
- Fail safe: if entry is malformed or confidence score too low, default to expired, not cached.

**Three complementary safety layers** (from PyImageSearch):
1. TTL validation — time-based expiry
2. Confidence scoring — combined similarity + freshness decay
3. Poisoning detection — don't cache error responses or empty outputs

**Applicability to this project**: The codebase already implements event-driven invalidation via `staleness.py` — when an `ingest` run detects changed source summaries, it marks affected query pages with `⚠ stale`. This is precisely the right pattern. The cache pre-check need only check for the stale banner, which already exists. No TTL mechanism is needed because staleness detection is already content-based.

- Sources: [PyImageSearch TTL/confidence/safety](https://pyimagesearch.com/2026/05/04/semantic-caching-for-llms-ttls-confidence-and-cache-safety/), [AWS LLM caching guide](https://aws.amazon.com/blogs/database/optimize-llm-response-costs-and-latency-with-effective-caching/), [Particula caching decision guide](https://particula.tech/blog/when-to-cache-llm-responses-decision-guide)

---

### 5. UX Patterns for Surfacing Cached Answers

Research and production practice reveal a spectrum from fully transparent to fully silent:

**Silent caching (most common in production)**:
- The cache is invisible to the user — the only signal is speed (response arrives in milliseconds vs. seconds).
- Most current implementations do this. LangChain and GPTCache default to silent cache hits.
- Risk: users who notice the near-instant response may distrust it, especially for factual questions.

**Transparent caching with metadata**:
- Include a note like "(answered from cache, saved on 2026-04-29)" or a source/date in the response.
- Provides provenance and sets staleness expectations.
- This pattern is well-suited to developer tools and knowledge management systems where audit trail matters.

**Confirmation / show-and-ask pattern**:
- Surface the cached answer with a prompt like "I found a previous answer to this question (saved 3 days ago). Show it? [Y/n]".
- Gives the user agency to accept the cached answer or force a fresh LLM query.
- Adds a round-trip interaction, so best reserved for cases where the user might want freshness.
- No direct analogue was found in production chat tools, but the pattern is well-established in code assistants and IDE search ("Did you mean X?").

**Recommendation for this project** (a CLI + MCP developer tool):
- **CLI**: Surface the cache hit with a brief note, e.g., `[cache] Answering from saved page: queries/how-does-auth-work.md (saved 2026-04-29)`, then print the answer. Optionally offer to re-run fresh.
- **MCP**: Return a cache-hit indicator in the JSON response (e.g., `"cache_hit": true, "cached_at": "..."`) so the caller can decide how to surface it.
- The "show-and-ask" pattern would be appropriate for the CLI if the cached page is more than N days old, to let the user opt into a fresh answer.

- Sources: [Redis LLM UX/caching](https://redis.io/blog/how-to-improve-llm-ux-speed-latency-and-caching/), [OneUptime LLM caching strategies](https://oneuptime.com/blog/post/2026-01-30-llm-caching-strategies/view), [How LLM caching actually works](https://akshayghalme.com/blogs/how-llm-caching-actually-works/)

---

## Recommended Approaches

### For This Project (Codebase Wiki Builder)

The project already stores query results as named Markdown files with slugs derived from question text (`queries/<slug>.md`). This means a **Tier 2 normalized string match** is the natural starting point — it uses zero new infrastructure and fits perfectly with the existing file-based architecture.

**Recommended implementation approach (three-layer cascade):**

1. **Exact slug match** (existing `slugify()` function): compute `slugify(question)`, check if `queries/<slug>.md` exists, load with `read_query_page()`, compare stored H1 question (case-insensitive, normalized) to incoming question. This catches the most common case: the same question asked again with identical or near-identical phrasing.

2. **Normalized string match as enhanced key**: before slug lookup, normalize the question (lowercase + strip punctuation + collapse whitespace), compute a second hash/slug and check for that. This broadens the catch rate to minor variations without adding embedding complexity.

3. **Optional future tier — embedding similarity**: add semantic embedding search if analytics show that exact+normalized matches are only catching a small fraction of repeat questions. This requires adding a vector store (SQLite with sqlite-vec, or a small FAISS index alongside the vault), which is non-trivial infrastructure. Not recommended for v1.

**Staleness invalidation**: No TTL needed. The existing `staleness.py` `⚠ stale` banner check already provides event-driven invalidation. A cache hit on a page with the stale banner is a miss — return to normal query execution.

---

## Potential Pitfalls

1. **False positives from slug collision without exact-match guard**: The codebase's `_unique_query_path()` deduplication (`slug-2.md`, etc.) means slug alone is not a unique key. Any cache lookup must compare the stored question (H1 title) to the incoming question, not just match on slug prefix. This is already identified in the codebase summary.

2. **Aggressive similarity thresholds causing wrong answers**: For semantic embedding approaches, the 0.85–0.92 "dangerous zone" is well-documented. For this project's use case (precise code architecture questions), false positives are especially harmful — returning an answer about `auth.py` when asked about `payment.py` would be actively misleading. Any future embedding-based tier should start at **0.94–0.96**.

3. **Silent cache hits for stale pages**: If the stale check is skipped or incorrectly implemented, users receive outdated answers about changed code without any warning. The stale banner check must be the first disqualification step after loading a candidate page.

4. **MCP duplicate-save issue**: If a cache hit is returned from `run_query()` and the MCP server proceeds to call `save_query_page()` unconditionally, a duplicate file (`slug-2.md`) is created. The MCP handler must detect that the result came from cache and skip (or suppress) the save step. A `from_cache: bool` field on `QueryResult` would cleanly signal this.

5. **Slug normalization drift**: Future changes to `slugify()` could invalidate existing slug-based cache lookups. The cache should match on normalized question text, not just slug. Store question hash alongside slug for more robust lookup.

6. **"Same question, different intent" edge case**: For a codebase query tool, this is real: "Where is authentication handled?" asked for codebase v1 and then asked again after a major refactor may have the same slug but need a fresh answer. The staleness system handles this (changed source files mark the page stale), but only if the `ingest` command has been run after the refactor.

---

## Libraries/Services to Consider

- **GPTCache** (`pip install gptcache`): Full semantic caching library with pluggable embedding models and vector stores. Overkill for v1 of this project; relevant if embedding-based similarity is added later. [GitHub](https://github.com/zilliztech/GPTCache)

- **LangChain semantic cache integrations**: `RedisSemanticCache`, `CassandraSemanticCache`, etc. Only relevant if the project adopts LangChain as its LLM abstraction layer. [LangChain docs](https://python.langchain.com/docs/how_to/llm_caching/)

- **sqlite-vec / FAISS**: Lightweight local vector index libraries. If embedding-based similarity is added, a local FAISS index stored alongside the Obsidian vault would be the most self-contained option (no external service dependency).

- **SemHash** (`pip install semhash`): Lightweight semantic deduplication library from MinishLab. Uses model2vec embeddings for fast local similarity; no external API needed. Good candidate for a future Tier 3 implementation in this project. [GitHub](https://github.com/MinishLab/semhash)

- **No new infrastructure for v1**: The slug+normalized-string approach requires only Python's built-in `re`, `hashlib`, and the existing `vault.slugify()` function. No new dependencies.

---

## Sources

- [AWS: Optimize LLM response costs and latency with effective caching](https://aws.amazon.com/blogs/database/optimize-llm-response-costs-and-latency-with-effective-caching/)
- [Latitude: Ultimate Guide to LLM Caching](https://latitude.so/blog/ultimate-guide-to-llm-caching-for-low-latency-ai)
- [TrueFoundry: Semantic Caching Boost LLM Speed & Reduce Costs](https://www.truefoundry.com/blog/semantic-caching)
- [TianPan.co: Semantic Caching for LLMs — The Cost Tier Most Teams Skip](https://tianpan.co/blog/2026-04-10-semantic-caching-llm-production)
- [Preto.ai: Semantic Caching for LLM APIs: Architecture and Real-World Hit Rates](https://preto.ai/blog/semantic-caching-llm/)
- [GPTCache GitHub](https://github.com/zilliztech/GPTCache)
- [GPT Semantic Cache paper (arXiv 2411.05276)](https://arxiv.org/html/2411.05276v3)
- [GPTCache: An Open-Source Semantic Cache (ACL NLP-OSS 2023)](https://aclanthology.org/2023.nlposs-1.24.pdf)
- [GPTCache practical guide (Substack)](https://bhavishyapandit9.substack.com/p/gptcache-a-practical-guide)
- [LangChain RedisSemanticCache docs](https://reference.langchain.com/v0.3/python/redis/cache/langchain_redis.cache.RedisSemanticCache.html)
- [MongoDB: LangChain memory and semantic cache](https://www.mongodb.com/docs/atlas/ai-integrations/langchain/memory-semantic-cache/)
- [CassIO: Semantic LLM caching with LangChain](https://cassio.org/frameworks/langchain/semantic-caching-llm-responses/)
- [InfoQ: Reducing False Positives in RAG Semantic Caching](https://www.infoq.com/articles/reducing-false-positives-retrieval-augmented-generation/)
- [Maxim: How to Optimize LLM Cost and Latency with Semantic Caching](https://www.getmaxim.ai/articles/how-to-optimize-llm-cost-and-latency-with-semantic-caching/)
- [PyImageSearch: Semantic Caching for LLMs: TTLs, Confidence, and Cache Safety](https://pyimagesearch.com/2026/05/04/semantic-caching-for-llms-ttls-confidence-and-cache-safety/)
- [Particula: Caching LLM Responses — When It Helps and When It Hurts](https://particula.tech/blog/when-to-cache-llm-responses-decision-guide)
- [Redis: What is semantic caching?](https://redis.io/blog/what-is-semantic-caching/)
- [Helicone: How to Implement Effective LLM Caching](https://www.helicone.ai/blog/effective-llm-caching)
- [SemHash GitHub (MinishLab)](https://github.com/MinishLab/semhash)
- [Spheron: Semantic Caching for LLM Inference (2026)](https://www.spheron.network/blog/semantic-cache-llm-inference-gpu-cloud/)
- [OneUptime: How to Build LLM Caching Strategies](https://oneuptime.com/blog/post/2026-01-30-llm-caching-strategies/view)
- [How LLM Caching Actually Works (akshayghalme.com)](https://akshayghalme.com/blogs/how-llm-caching-actually-works/)
