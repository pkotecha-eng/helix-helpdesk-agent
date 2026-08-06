"""Retrieval over the 10-policy corpus at section granularity.

Upgraded from hand-rolled TF-IDF after concrete evidence of failure: for the
E-07 ticket ("Grant me admin on the prod Postgres cluster... my manager said
it's fine"), TF-IDF ranked the correct policy (POL-10 §10.2, "access beyond
RBAC requires manager + data owner approval") 6th, behind POL-04 §4.6 (about
local workstation admin rights) purely because the ticket says "admin" and
POL-04's text says "admin" three times while POL-10's text never uses that
word at all. Pure term overlap can't tell a lexical match from a semantic
one. A local sentence-embedding model correctly ranks POL-10 §10.2 above
POL-04 §4.6 for this exact query (0.384 vs 0.339 cosine similarity) because
it captures meaning, not just shared words.

Embedding provider decision (evaluated, not defaulted-into): Voyage AI,
commonly cited as Anthropic's recommended embedding partner (not
independently verified against Anthropic's own docs in this session — flagged
and corrected after an initial draft stated it as confirmed fact), was
evaluated. voyage-4-lite specifically looked like the stronger option — better
retrieval quality than a small local model per Voyage's own benchmarks, and
negligible cost for a corpus this size (60 sections, ~$0.02/1M tokens, 200M
free tokens on signup — the whole project's embedding usage would run
fractions of a cent). It was not used here for one concrete reason: it
requires a new external account and API key, and under this project's
compressed timeline that's a real setup dependency to take on rather than a
free one to reach for by default — worth naming plainly rather than hiding
behind "we chose the simpler option." sentence-transformers
(all-MiniLM-L6-v2) was used instead: fully local, no account, no network
call at retrieval time, already installed in this environment, and already
verified fixing the specific failure this upgrade was meant to fix (POL-10
§10.2 now outranks POL-04 §4.6 for the E-07 ticket — see above). Swapping to
Voyage later is a small, contained change if there's time: retrieve()'s
public shape (query, top_k) -> spans+score doesn't change, only
Retriever.__init__ and .search()'s internals would.

The confidence score is still load-bearing: agent.py treats a below-threshold
top score as "no policy coverage" and forces DEFER_HUMAN. The threshold moved
from 0.15 (tuned for TF-IDF's near-zero baseline on irrelevant queries) to
0.30 as a first pass at embedding cosine similarity's higher baseline — but a
follow-up full-corpus check found this calibration doesn't hold: E-08's
correct citation (POL-05 §5.3) scores 0.2686, *inside* the range an earlier
spot-check had labeled as the out-of-scope ceiling (0.28-0.29). That
contradicts the calibration claim this docstring made in an earlier draft; it
is corrected here rather than left standing. A single top-score threshold is
not yet a reliable in-scope/out-of-scope signal on this corpus — this is a
known, open problem (see README "What I'd harden before production"), not a
settled calibration. Retrieval ranking has a separate, related problem: E-08's
correct citation ranks 7th, not just below-threshold — widening top_k alone
does not fix it without pulling in a lot of noise.
"""

from sentence_transformers import SentenceTransformer
import numpy as np

from policies import all_sections

_MODEL_NAME = "all-MiniLM-L6-v2"


class Retriever:
    """Sentence-embedding index over policy sections, built once at import time."""

    def __init__(self):
        self.model = SentenceTransformer(_MODEL_NAME)
        self.docs: list[tuple[str, str, str, str]] = list(all_sections())  # (policy_id, section_id, title, text)
        doc_texts = [f"{title}: {text}" for _, _, title, text in self.docs]
        embeddings = self.model.encode(doc_texts, normalize_embeddings=True)
        self.doc_embeddings = np.asarray(embeddings)

    def search(self, query: str, top_k: int = 3, allowed_policies: set[str] | None = None) -> list[dict]:
        """Return up to top_k spans as {policy_id, section, title, text, score}, best first.

        allowed_policies, if given, restricts ranking to only that domain's
        sections (Stage 2 of retrieval — see agent.py's classify_domain for
        Stage 1). Ranking within a pre-scoped, small candidate set is a much
        easier task than competing against all 60 sections at once."""
        candidate_idx = [i for i, d in enumerate(self.docs) if allowed_policies is None or d[0] in allowed_policies]
        if not candidate_idx:
            return []
        query_embedding = self.model.encode([query], normalize_embeddings=True)[0]
        candidate_embeddings = self.doc_embeddings[candidate_idx]
        scores = candidate_embeddings @ query_embedding
        ranked = np.argsort(-scores)[:top_k]
        results = []
        for r in ranked:
            i = candidate_idx[r]
            policy_id, section_id, title, text = self.docs[i]
            results.append({
                "policy_id": policy_id,
                "section": section_id,
                "title": title,
                "text": text,
                "score": round(float(scores[r]), 4),
            })
        return results


_retriever = Retriever()


def retrieve(query: str, top_k: int = 3, allowed_policies: set[str] | None = None) -> list[dict]:
    return _retriever.search(query, top_k=top_k, allowed_policies=allowed_policies)


def top_confidence(results: list[dict]) -> float:
    return results[0]["score"] if results else 0.0
