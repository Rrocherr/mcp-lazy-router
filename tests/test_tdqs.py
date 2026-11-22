"""Unit tests for TDQS scoring, safety inference, and tool normalization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_lazy_router.server import get_meta_tools
from mcp_lazy_router.tdqs import infer_safety, normalize_tool, score_tool_tdqs


class TDQSTests(unittest.TestCase):
    def test_empty_and_trivial_descriptions_score_low(self) -> None:
        empty_tool = {"name": "syncData", "description": "", "inputSchema": {"properties": {"x": {}}}}
        rep_empty = score_tool_tdqs(empty_tool)
        self.assertLess(rep_empty.score, 25)
        self.assertEqual(rep_empty.grade, "F")
        self.assertIn("Missing tool description", rep_empty.smells)

        echo_tool = {
            "name": "getUser",
            "description": "Get user",
            "inputSchema": {"properties": {"userId": {"type": "string"}}},
        }
        rep_echo = score_tool_tdqs(echo_tool)
        self.assertLess(rep_echo.score, 40)
        self.assertIn("Trivial or name-echoing description", rep_echo.smells)

    def test_high_quality_tool_scores_grade_a(self) -> None:
        good_tool = {
            "name": "search_issues",
            "description": (
                "Search repository issues using GitHub search syntax and pagination. "
                "Use when locating matching bug reports or open tickets. "
                "Do not use to fetch full comment threads; limited to 100 results."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Issue search expression."},
                    "limit": {"type": "integer", "description": "Maximum issues per page."},
                },
                "required": ["query"],
            },
            "outputSchema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        }
        report = score_tool_tdqs(good_tool)
        self.assertEqual(report.score, 100)
        self.assertEqual(report.grade, "A")
        self.assertEqual(report.smells, [])

    def test_router_meta_tools_all_score_100_grade_a(self) -> None:
        meta_tools = get_meta_tools(include_output_schema=True)
        self.assertEqual(len(meta_tools), 4)
        for tool in meta_tools:
            report = score_tool_tdqs(tool)
            self.assertEqual(
                report.score,
                100,
                f"Meta-tool {tool['name']} scored {report.score}: smells={report.smells}",
            )
            self.assertEqual(report.grade, "A")

    def test_safety_inference_declared_vs_heuristics(self) -> None:
        # Declared annotations override heuristics
        ro, dest, src = infer_safety({
            "name": "delete_cache_preview",
            "annotations": {"readOnlyHint": True, "destructiveHint": False},
        })
        self.assertEqual((ro, dest, src), (True, False, "declared"))

        # Destructive verb heuristic
        self.assertEqual(infer_safety({"name": "deleteRepository"}), (False, True, "inferred"))
        self.assertEqual(infer_safety({"name": "forcePushBranch"}), (False, True, "inferred"))

        # Non-destructive push is mutating, not destructive
        self.assertEqual(infer_safety({"name": "push_branch_changes"}), (False, False, "inferred"))

        # Read-only heuristic vs compound mutating verb
        self.assertEqual(infer_safety({"name": "list_repositories"}), (True, False, "inferred"))
        self.assertEqual(infer_safety({"name": "get_and_update_user"}), (False, False, "inferred"))

    def test_normalize_tool_fallback_without_hallucination(self) -> None:
        norm = normalize_tool(
            "crm",
            {
                "name": "purgeOldRecords",
                "description": "",
                "inputSchema": {
                    "type": "object",
                    "properties": {"tenant_id": {"type": "string"}},
                    "required": ["tenant_id"],
                },
            },
            tags=["billing"],
        )
        self.assertTrue(norm.compact_summary.startswith("[auto-summary]"))
        self.assertIn("tenant_id", norm.compact_summary)
        self.assertTrue(norm.inferred_destructive)
        self.assertEqual(norm.safety_source, "inferred")
        self.assertIn("TDQS:", norm.normalized_description)
        self.assertIn("Required params: tenant_id:string", norm.normalized_description)


if __name__ == "__main__":
    unittest.main()
