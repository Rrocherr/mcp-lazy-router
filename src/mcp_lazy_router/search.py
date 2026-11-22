"""Field-weighted Okapi BM25 + synonym search engine for normalized MCP tools."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .tdqs import NormalizedTool, split_identifier

_KEEP_SHORT_TOKENS = frozenset({
    "id",
    "pr",
    "db",
    "fs",
    "ui",
    "io",
    "ip",
    "mr",
    "os",
    "ai",
    "rm",
    "ls",
    "gh",
    "ci",
    "cd",
})

_STOPWORDS = frozenset({
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "onto",
    "your",
    "when",
    "what",
    "which",
    "only",
    "does",
    "have",
    "will",
})

SYNONYMS: dict[str, tuple[str, ...]] = {
    "pr": ("pull",),
    "pull": ("pr",),
    "issue": ("ticket", "bug"),
    "ticket": ("issue", "bug"),
    "bug": ("issue", "ticket"),
    "mail": ("email",),
    "email": ("mail",),
    "db": ("database", "sql"),
    "database": ("db", "sql"),
    "sql": ("db", "database"),
    "repo": ("repository", "git"),
    "repository": ("repo", "git"),
    "git": ("repo", "repository"),
    "msg": ("message", "chat"),
    "message": ("msg", "chat"),
    "chat": ("msg", "message"),
    "doc": ("document",),
    "document": ("doc",),
    "dir": ("directory", "folder"),
    "directory": ("dir", "folder"),
    "folder": ("dir", "directory"),
    "del": ("delete", "remove"),
    "delete": ("del", "remove"),
    "remove": ("del", "delete"),
    "auth": ("login", "token", "oauth"),
    "login": ("auth", "token", "oauth"),
    "token": ("auth", "login", "oauth"),
    "oauth": ("auth", "login", "token"),
    "cal": ("calendar", "event"),
    "calendar": ("cal", "event"),
    "event": ("cal", "calendar"),
}


def _stem_token(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 5 and tok.endswith(("ches", "shes", "xes", "sses", "tuses")):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def tokenize(text: str) -> list[str]:
    """Tokenize free text and identifiers into normalized searchable stems."""
    if not text:
        return []
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9_.-]+", text):
        for piece in split_identifier(raw):
            stemmed = _stem_token(piece)
            if stemmed in _STOPWORDS:
                continue
            if len(stemmed) >= 3 or stemmed in _KEEP_SHORT_TOKENS:
                tokens.append(stemmed)
    return tokens


def _extract_param_tokens(schema: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    props = schema.get("properties")
    if isinstance(props, dict):
        for p_name, p_def in props.items():
            tokens.extend(tokenize(str(p_name)))
            if isinstance(p_def, dict):
                p_desc = p_def.get("description")
                if isinstance(p_desc, str):
                    tokens.extend(tokenize(p_desc))
    return tokens


class ToolSearchIndex:
    """In-memory field-weighted BM25 + synonym index over NormalizedTool objects."""

    _FIELD_WEIGHTS: dict[str, float] = {
        "name": 4.0,
        "server_tags": 2.5,
        "description": 2.0,
        "params": 1.2,
    }
    _K1 = 1.5
    _B = 0.75

    def __init__(self, tools: list[NormalizedTool] | None = None) -> None:
        self._tools: list[NormalizedTool] = []
        self._field_tfs: dict[str, list[Counter[str]]] = {k: [] for k in self._FIELD_WEIGHTS}
        self._field_lens: dict[str, list[int]] = {k: [] for k in self._FIELD_WEIGHTS}
        self._avg_lens: dict[str, float] = {k: 1.0 for k in self._FIELD_WEIGHTS}
        self._dfs: dict[str, Counter[str]] = {k: Counter() for k in self._FIELD_WEIGHTS}
        self.rebuild(tools or [])

    def rebuild(self, tools: list[NormalizedTool]) -> None:
        """Rebuild all field-weighted inverted indexes from a list of NormalizedTool."""
        self._tools = list(tools)
        self._field_tfs = {k: [] for k in self._FIELD_WEIGHTS}
        self._field_lens = {k: [] for k in self._FIELD_WEIGHTS}
        self._dfs = {k: Counter() for k in self._FIELD_WEIGHTS}

        for tool in self._tools:
            name_toks = tokenize(tool.name)
            server_tag_toks = tokenize(tool.server) + [
                t for tag in tool.tags for t in tokenize(tag)
            ]
            desc_toks = tokenize(tool.raw_description or tool.compact_summary)
            param_toks = _extract_param_tokens(tool.input_schema)

            field_map = {
                "name": name_toks,
                "server_tags": server_tag_toks,
                "description": desc_toks,
                "params": param_toks,
            }
            for fname, ftoks in field_map.items():
                counts = Counter(ftoks)
                self._field_tfs[fname].append(counts)
                self._field_lens[fname].append(len(ftoks))
                for term in counts:
                    self._dfs[fname][term] += 1

        n_docs = len(self._tools)
        for fname in self._FIELD_WEIGHTS:
            total_len = sum(self._field_lens[fname])
            self._avg_lens[fname] = (total_len / n_docs) if (n_docs > 0 and total_len > 0) else 1.0

    def _bm25_term_field(self, doc_idx: int, fname: str, term: str) -> float:
        tf = self._field_tfs[fname][doc_idx].get(term, 0)
        if tf == 0:
            return 0.0
        n_docs = len(self._tools)
        df = self._dfs[fname].get(term, 0)
        idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        doc_len = self._field_lens[fname][doc_idx]
        avg_len = self._avg_lens[fname] or 1.0
        denom = tf + self._K1 * (1.0 - self._B + self._B * (doc_len / avg_len))
        return idf * (tf * (self._K1 + 1.0)) / denom

    def _score_tool(
        self,
        doc_idx: int,
        raw_query_lower: str,
        query_terms: list[str],
        synonym_terms: list[str],
    ) -> float:
        score = 0.0
        for term in query_terms:
            for fname, weight in self._FIELD_WEIGHTS.items():
                score += weight * self._bm25_term_field(doc_idx, fname, term)

        for syn in synonym_terms:
            for fname, weight in self._FIELD_WEIGHTS.items():
                score += 0.45 * weight * self._bm25_term_field(doc_idx, fname, syn)

        tool = self._tools[doc_idx]
        name_lower = tool.name.lower()
        qname_lower = tool.qualified_name.lower()
        if raw_query_lower == name_lower or raw_query_lower == qname_lower:
            score += 12.0
        elif len(raw_query_lower) >= 3 and (
            raw_query_lower in name_lower or raw_query_lower in qname_lower
        ):
            score += 4.5
        elif len(raw_query_lower) >= 3 and raw_query_lower == tool.server.lower():
            score += 3.0

        return score

    def search(
        self,
        query: str,
        server: str | None = None,
        read_only_only: bool = False,
        min_tdqs: int | None = None,
        limit: int = 8,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search tools using BM25 + synonym expansion, or browse all tools with '*' / ''."""
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))

        raw_q = (query or "").strip()
        is_browse = raw_q in ("", "*")

        eligible_indices: list[int] = []
        for idx, tool in enumerate(self._tools):
            if server and tool.server != server:
                continue
            if read_only_only and not tool.inferred_read_only:
                continue
            if min_tdqs is not None and tool.tdqs.score < min_tdqs:
                continue
            eligible_indices.append(idx)

        if is_browse:
            eligible_indices.sort(key=lambda i: (self._tools[i].server, self._tools[i].name))
            scored: list[tuple[int, float]] = [(i, 1.0) for i in eligible_indices]
        else:
            query_terms = list(dict.fromkeys(tokenize(raw_q)))
            query_set = set(query_terms)
            synonym_terms: list[str] = []
            for qt in query_terms:
                for syn in SYNONYMS.get(qt, ()):
                    stemmed_syn = _stem_token(syn)
                    if stemmed_syn not in query_set and stemmed_syn not in synonym_terms:
                        synonym_terms.append(stemmed_syn)

            raw_lower = raw_q.lower()
            ranked: list[tuple[int, float]] = []
            for idx in eligible_indices:
                sc = self._score_tool(idx, raw_lower, query_terms, synonym_terms)
                if sc > 0.0:
                    ranked.append((idx, sc))

            ranked.sort(
                key=lambda item: (
                    -item[1],
                    -self._tools[item[0]].tdqs.score,
                    self._tools[item[0]].server,
                    self._tools[item[0]].name,
                )
            )
            scored = ranked

        total_matches = len(scored)
        page = scored[offset : offset + limit]

        results: list[dict[str, Any]] = []
        for idx, sc in page:
            t = self._tools[idx]
            results.append({
                "server": t.server,
                "tool": t.name,
                "qualified_name": t.qualified_name,
                "summary": t.compact_summary,
                "read_only": t.inferred_read_only,
                "destructive": t.inferred_destructive,
                "safety_source": t.safety_source,
                "tdqs_score": t.tdqs.score,
                "tdqs_grade": t.tdqs.grade,
                "relevance": round(sc, 4),
            })

        return {
            "total_matches": total_matches,
            "offset": offset,
            "limit": limit,
            "results": results,
        }
