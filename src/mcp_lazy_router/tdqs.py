"""Tool Description Quality Score (TDQS) auditing and non-hallucinatory normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .protocol import estimate_tokens

DESTRUCTIVE_VERBS = frozenset({
    "delete",
    "remove",
    "drop",
    "destroy",
    "purge",
    "truncate",
    "wipe",
    "reset",
    "kill",
    "terminate",
    "revoke",
    "rm",
    "erase",
})

MUTATING_VERBS = frozenset({
    "create",
    "add",
    "insert",
    "update",
    "edit",
    "patch",
    "modify",
    "write",
    "set",
    "put",
    "post",
    "send",
    "publish",
    "merge",
    "close",
    "reopen",
    "approve",
    "assign",
    "run",
    "exec",
    "execute",
    "trigger",
    "start",
    "stop",
    "restart",
    "deploy",
    "install",
    "uninstall",
    "upload",
    "move",
    "rename",
    "copy",
    "clone",
    "fork",
    "save",
    "clear",
    "push",
    "cancel",
    "archive",
})

READ_ONLY_VERBS = frozenset({
    "get",
    "list",
    "search",
    "find",
    "read",
    "fetch",
    "query",
    "describe",
    "inspect",
    "show",
    "view",
    "check",
    "status",
    "count",
    "stat",
    "info",
    "lookup",
    "resolve",
    "health",
    "ping",
    "audit",
    "analyze",
    "preview",
    "diff",
    "echo",
})

_USAGE_CUES = re.compile(
    r"\b(use when|when to use|use this|intended for|ideal for|useful for|best for|call this when|for example|e\.g\.)\b",
    re.IGNORECASE,
)

_BOUNDARY_CUES = re.compile(
    r"\b(do not use|don't use|when not to use|not for|avoid|instead of|instead use|only when|requires|prerequisite|limit|limited to|max(?:imum)?|at most|read-only|does not|won't|cannot|can't|irreversible|permanently|warning|caution|side effect|rate limit)\b",
    re.IGNORECASE,
)

_OUTPUT_CUES = re.compile(
    r"\b(returns?|outputs?|yields?|responds? with|json|markdown|list of|array of|object containing|paginated)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class TDQSReport:
    """Audit report evaluating MCP tool metadata quality on a 0-100 scale."""

    score: int
    grade: str
    smells: list[str] = field(default_factory=list)
    breakdown: dict[str, int] = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "smells": list(self.smells),
            "breakdown": dict(self.breakdown),
            "suggestions": list(self.suggestions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TDQSReport:
        return cls(
            score=int(data["score"]),
            grade=str(data["grade"]),
            smells=list(data.get("smells", [])),
            breakdown={str(k): int(v) for k, v in dict(data.get("breakdown", {})).items()},
            suggestions=list(data.get("suggestions", [])),
        )


@dataclass(slots=True)
class NormalizedTool:
    """Indexed, normalized, and TDQS-annotated representation of an upstream MCP tool."""

    server: str
    name: str
    qualified_name: str
    raw_description: str
    compact_summary: str
    normalized_description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]
    inferred_read_only: bool
    inferred_destructive: bool
    safety_source: str
    tdqs: TDQSReport
    raw_token_estimate: int
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "raw_description": self.raw_description,
            "compact_summary": self.compact_summary,
            "normalized_description": self.normalized_description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "annotations": self.annotations,
            "inferred_read_only": self.inferred_read_only,
            "inferred_destructive": self.inferred_destructive,
            "safety_source": self.safety_source,
            "tdqs": self.tdqs.to_dict(),
            "raw_token_estimate": self.raw_token_estimate,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NormalizedTool:
        return cls(
            server=str(data["server"]),
            name=str(data["name"]),
            qualified_name=str(data["qualified_name"]),
            raw_description=str(data.get("raw_description", "")),
            compact_summary=str(data.get("compact_summary", "")),
            normalized_description=str(data.get("normalized_description", "")),
            input_schema=dict(data.get("input_schema") or {}),
            output_schema=dict(data["output_schema"]) if data.get("output_schema") else None,
            annotations=dict(data.get("annotations") or {}),
            inferred_read_only=bool(data.get("inferred_read_only", False)),
            inferred_destructive=bool(data.get("inferred_destructive", False)),
            safety_source=str(data.get("safety_source", "inferred")),
            tdqs=TDQSReport.from_dict(data["tdqs"]),
            raw_token_estimate=int(data.get("raw_token_estimate", 0)),
            tags=list(data.get("tags", [])),
        )


def split_identifier(name: str) -> list[str]:
    """Split camelCase, PascalCase, snake_case, kebab-case, and paths into lowercased tokens."""
    if not name:
        return []
    tokens: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", name):
        if not chunk:
            continue
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", chunk)
        spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
        for part in spaced.split():
            lowered = part.lower()
            if lowered:
                tokens.append(lowered)
    return tokens


def infer_safety(tool: dict[str, Any]) -> tuple[bool, bool, str]:
    """Determine (read_only, destructive, source) from MCP annotations or verb heuristics."""
    annotations = tool.get("annotations")
    if isinstance(annotations, dict):
        has_ro = isinstance(annotations.get("readOnlyHint"), bool)
        has_dest = isinstance(annotations.get("destructiveHint"), bool)
        if has_ro or has_dest:
            destructive = bool(annotations.get("destructiveHint", False))
            if has_ro:
                read_only = bool(annotations["readOnlyHint"]) and not destructive
            else:
                read_only = False
            return read_only, destructive, "declared"

    name = str(tool.get("name", ""))
    tokens = split_identifier(name)
    token_set = set(tokens)

    # Detect force_push / forcePush bigram as destructive without flagging normal push
    has_force_push = any(
        tokens[i] == "force" and tokens[i + 1] == "push"
        for i in range(len(tokens) - 1)
    )
    if has_force_push or (token_set & DESTRUCTIVE_VERBS):
        return False, True, "inferred"

    if token_set & MUTATING_VERBS:
        return False, False, "inferred"

    if token_set & READ_ONLY_VERBS:
        return True, False, "inferred"

    return False, False, "inferred"


def _grade_from_score(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def score_tool_tdqs(tool: dict[str, Any]) -> TDQSReport:
    """Evaluate a raw MCP tool dictionary across the 100-point TDQS rubric."""
    name = str(tool.get("name", "")).strip()
    desc = str(tool.get("description") or "").strip()
    input_schema = tool.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {}
    output_schema = tool.get("outputSchema")
    annotations = tool.get("annotations")

    smells: list[str] = []
    suggestions: list[str] = []

    # 1. Purpose clarity (20 pts)
    name_tokens = set(split_identifier(name))
    desc_tokens = set(split_identifier(desc))
    novel_tokens = desc_tokens - name_tokens - {"a", "an", "the", "to", "for", "of", "and", "or", "in"}

    if not desc:
        purpose_pts = 0
        smells.append("Missing tool description")
        suggestions.append("Add a clear description explaining the tool's purpose and action.")
    elif len(desc) < 25 or len(novel_tokens) <= 1:
        purpose_pts = 6
        smells.append("Trivial or name-echoing description")
        suggestions.append("Expand description beyond repeating the tool name (>= 25 chars).")
    elif len(desc) < 40:
        purpose_pts = 15
    else:
        purpose_pts = 20

    # 2. Usage guidance (15 pts)
    if _USAGE_CUES.search(desc):
        usage_pts = 15
    elif len(desc) >= 60:
        usage_pts = 7
        smells.append("Missing explicit when-to-use guidance")
        suggestions.append("Include a 'Use when:' clause explaining ideal invocation scenarios.")
    else:
        usage_pts = 0
        smells.append("Missing explicit when-to-use guidance")
        suggestions.append("Document when the model should select this tool.")

    # 3. Boundary guidance (15 pts)
    if _BOUNDARY_CUES.search(desc):
        boundary_pts = 15
    else:
        boundary_pts = 0
        smells.append("Missing boundary / when-not-to-use / limit guidance")
        suggestions.append("Clarify limits, prerequisites, side effects, or when NOT to use this tool.")

    # 4. Parameter documentation (20 pts)
    props = input_schema.get("properties")
    if not isinstance(props, dict) or len(props) == 0:
        param_pts = 20
    else:
        total_props = len(props)
        desc_count = 0
        typed_count = 0
        for prop_schema in props.values():
            if not isinstance(prop_schema, dict):
                continue
            if str(prop_schema.get("description") or "").strip():
                desc_count += 1
            if any(k in prop_schema for k in ("type", "enum", "anyOf", "oneOf", "const")):
                typed_count += 1
        desc_pts = round(12 * desc_count / total_props)
        type_pts = round(8 * typed_count / total_props)
        param_pts = desc_pts + type_pts
        if desc_count < total_props:
            smells.append(f"Undocumented input parameters ({total_props - desc_count}/{total_props})")
            suggestions.append("Add concise 'description' fields to every input property.")
        if typed_count < total_props:
            smells.append(f"Untyped input parameters ({total_props - typed_count}/{total_props})")
            suggestions.append("Declare explicit 'type' or 'enum' constraints on all parameters.")

    # 5. Safety annotations (15 pts)
    if isinstance(annotations, dict) and (
        isinstance(annotations.get("readOnlyHint"), bool)
        or isinstance(annotations.get("destructiveHint"), bool)
    ):
        safety_pts = 15
    elif isinstance(annotations, dict) and len(annotations) > 0:
        safety_pts = 8
        smells.append("Missing readOnlyHint/destructiveHint annotations")
        suggestions.append("Declare explicit readOnlyHint and destructiveHint booleans in annotations.")
    else:
        safety_pts = 0
        smells.append("Missing ToolAnnotations safety hints")
        suggestions.append("Add MCP ToolAnnotations (readOnlyHint, destructiveHint, idempotentHint).")

    # 6. Output contract (15 pts)
    if isinstance(output_schema, dict) and len(output_schema) > 0:
        output_pts = 15
    elif desc and _OUTPUT_CUES.search(desc):
        output_pts = 8
        smells.append("Missing formal outputSchema")
        suggestions.append("Define a structured JSON outputSchema for predictable downstream parsing.")
    else:
        output_pts = 0
        smells.append("Missing outputSchema and return format contract")
        suggestions.append("Add outputSchema or document the return payload structure.")

    breakdown = {
        "purpose_clarity": purpose_pts,
        "usage_guidance": usage_pts,
        "boundary_guidance": boundary_pts,
        "parameter_docs": param_pts,
        "safety_annotations": safety_pts,
        "output_contract": output_pts,
    }
    total = sum(breakdown.values())
    return TDQSReport(
        score=total,
        grade=_grade_from_score(total),
        smells=smells,
        breakdown=breakdown,
        suggestions=suggestions,
    )


def _build_compact_summary(name: str, raw_desc: str, input_schema: dict[str, Any]) -> str:
    collapsed = " ".join(raw_desc.split())
    if collapsed:
        # Take first sentence if clear, otherwise prefix cleanly
        first_sentence = re.split(r"(?<=[.!?])\s+", collapsed, maxsplit=1)[0]
        candidate = first_sentence if len(first_sentence) >= 20 else collapsed
        if len(candidate) <= 140:
            return candidate
        return candidate[:137].rstrip() + "..."

    tokens = split_identifier(name)
    action_label = " ".join(tokens) if tokens else name
    required = input_schema.get("required")
    req_list = [str(x) for x in required if isinstance(x, str)] if isinstance(required, list) else []
    if req_list:
        fallback = f"[auto-summary] {action_label} (requires: {', '.join(req_list[:4])})"
    else:
        props = input_schema.get("properties")
        prop_names = list(props.keys())[:4] if isinstance(props, dict) else []
        if prop_names:
            fallback = f"[auto-summary] {action_label} (params: {', '.join(prop_names)})"
        else:
            fallback = f"[auto-summary] {action_label}"
    return fallback[:140] if len(fallback) <= 140 else fallback[:137] + "..."


def normalize_tool(
    server_name: str,
    raw_tool: dict[str, Any],
    tags: list[str] | None = None,
) -> NormalizedTool:
    """Normalize a raw upstream MCP tool dict into an indexed NormalizedTool with TDQS audit."""
    name = str(raw_tool.get("name", "")).strip()
    raw_desc = str(raw_tool.get("description") or "").strip()

    input_schema = raw_tool.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}}
    output_schema = raw_tool.get("outputSchema")
    if not isinstance(output_schema, dict):
        output_schema = None
    annotations = raw_tool.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}

    read_only, destructive, safety_source = infer_safety(raw_tool)
    tdqs = score_tool_tdqs(raw_tool)
    compact_summary = _build_compact_summary(name, raw_desc, input_schema)

    props = input_schema.get("properties")
    prop_map = props if isinstance(props, dict) else {}
    required_raw = input_schema.get("required")
    required_set = set(required_raw) if isinstance(required_raw, list) else set()

    req_params: list[str] = []
    opt_params: list[str] = []
    for p_name, p_schema in prop_map.items():
        p_type = p_schema.get("type", "any") if isinstance(p_schema, dict) else "any"
        label = f"{p_name}:{p_type}"
        if p_name in required_set:
            req_params.append(label)
        else:
            opt_params.append(label)

    if destructive:
        safety_label = f"destructive (confirmation required) [{safety_source}]"
    elif read_only:
        safety_label = f"read-only [{safety_source}]"
    else:
        safety_label = f"mutating [{safety_source}]"

    lines = [raw_desc if raw_desc else compact_summary]
    if req_params:
        lines.append(f"Required params: {', '.join(req_params)}")
    if opt_params:
        lines.append(f"Optional params: {', '.join(opt_params)}")
    lines.append(f"Safety: {safety_label}")
    lines.append(f"TDQS: {tdqs.score}/100 (Grade {tdqs.grade})")
    if tdqs.smells:
        lines.append(f"Quality notes: {'; '.join(tdqs.smells[:3])}")

    normalized_desc = "\n".join(lines)
    raw_tokens = estimate_tokens(raw_tool)

    return NormalizedTool(
        server=server_name,
        name=name,
        qualified_name=f"{server_name}/{name}",
        raw_description=raw_desc,
        compact_summary=compact_summary,
        normalized_description=normalized_desc,
        input_schema=dict(input_schema),
        output_schema=dict(output_schema) if output_schema is not None else None,
        annotations=dict(annotations),
        inferred_read_only=read_only,
        inferred_destructive=destructive,
        safety_source=safety_source,
        tdqs=tdqs,
        raw_token_estimate=raw_tokens,
        tags=list(tags or []),
    )
