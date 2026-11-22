"""Unit tests for hybrid field-weighted BM25 + synonym tool search."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_lazy_router.search import ToolSearchIndex, tokenize
from mcp_lazy_router.tdqs import normalize_tool


def _sample_tools():
    raw_specs = [
        ("github", ["git", "vcs"], {
            "name": "getPullRequest",
            "description": "Retrieve pull request status, commits, and review approvals.",
            "inputSchema": {"type": "object", "properties": {"prNumber": {"type": "integer"}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }),
        ("github", ["git", "vcs"], {
            "name": "create_issue",
            "description": "Open a new tracking ticket in a repository.",
            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        }),
        ("postgres", ["sql", "storage"], {
            "name": "run_sql_query",
            "description": "Execute a read-only SELECT statement against the analytics database.",
            "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }),
        ("slack", ["messaging"], {
            "name": "post_channel_message",
            "description": "Send a message to a Slack channel.",
            "inputSchema": {"type": "object", "properties": {"channel": {"type": "string"}}},
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        }),
    ]
    return [normalize_tool(srv, raw, tags=tags) for srv, tags, raw in raw_specs]


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = ToolSearchIndex(_sample_tools())

    def test_tokenize_splits_identifiers_and_keeps_short_tech_tokens(self) -> None:
        tokens = tokenize("getPR_ByID_dbSQL")
        self.assertIn("get", tokens)
        self.assertIn("pr", tokens)
        self.assertIn("id", tokens)
        self.assertIn("db", tokens)
        self.assertIn("sql", tokens)

    def test_bm25_exact_and_camel_case_ranking(self) -> None:
        res = self.index.search("pull request")
        self.assertGreater(res["total_matches"], 0)
        self.assertEqual(res["results"][0]["tool"], "getPullRequest")

    def test_synonym_expansion_recall(self) -> None:
        # 'pr' should match getPullRequest; 'db' should match run_sql_query; 'bug' -> create_issue
        pr_res = self.index.search("pr")
        self.assertEqual(pr_res["results"][0]["tool"], "getPullRequest")

        db_res = self.index.search("db")
        self.assertEqual(db_res["results"][0]["tool"], "run_sql_query")

        bug_res = self.index.search("bug")
        self.assertTrue(any(r["tool"] == "create_issue" for r in bug_res["results"]))

    def test_filters_and_pagination(self) -> None:
        ro = self.index.search("*", read_only_only=True)
        self.assertEqual(ro["total_matches"], 2)
        self.assertTrue(all(r["read_only"] for r in ro["results"]))

        srv = self.index.search("*", server="github")
        self.assertEqual(srv["total_matches"], 2)
        self.assertTrue(all(r["server"] == "github" for r in srv["results"]))

        page1 = self.index.search("*", limit=2, offset=0)
        page2 = self.index.search("*", limit=2, offset=2)
        self.assertEqual(page1["total_matches"], 4)
        self.assertEqual(len(page1["results"]), 2)
        self.assertEqual(len(page2["results"]), 2)
        names1 = {r["qualified_name"] for r in page1["results"]}
        names2 = {r["qualified_name"] for r in page2["results"]}
        self.assertTrue(names1.isdisjoint(names2))


if __name__ == "__main__":
    unittest.main()
