"""
agent_tools.py — Facade module.

Re-exports tool parsing, schemas, execution, and implementations
for backward compatibility. All importers continue to work unchanged.

Sub-modules:
  - tool_parsing.py: regex patterns, parse/strip functions
  - tool_schemas.py: FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
  - tool_execution.py: execute_tool_block, format_tool_result, MCP helpers
  - tool_implementations.py: all do_* tool functions
"""

import logging
from collections import namedtuple

from src.tool_security import BUILTIN_EMAIL_TOOLS
from src.tool_utils import _truncate, get_mcp_manager, set_mcp_manager

logger = logging.getLogger(__name__)

from .subprocess_tools import BashTool, PythonTool, PowerShellTool
from .web_tools import WebSearchTool, WebFetchTool
from .filesystem_tools import ReadFileTool, WriteFileTool, EditFileTool, ApplyPatchTool, LsTool, GlobTool, GrepTool, GetWorkspaceTool
from .coding_tools import TodoWriteTool
from .subagent_tools import DelegateAgentsTool
from .code_tools import FindSymbolTool, CallersTool, TestsForTool, RenameSymbolTool
from .document_tools import CreateDocumentTool, UpdateDocumentTool, EditDocumentTool, SuggestDocumentTool, ManageDocumentTool
from .interaction_tools import AskUserTool, UpdatePlanTool, LookupToolsTool
from .model_interaction_tools import ChatWithModelTool, AskTeacherTool, ListModelsTool
from .media_tools import InspectMediaTool, MediaTransformTool
from .bg_job_tools import ManageBgJobsTool
from .session_tools import CreateSessionTool, ListSessionsTool, SendToSessionTool, ManageSessionTool
from .admin_tools import (
    ADMIN_TOOL_HANDLERS,
    do_manage_endpoints, do_manage_mcp, do_manage_webhooks,
    do_manage_tokens, do_manage_settings,
)
from .desktop_tools import DESKTOP_TOOL_HANDLERS, DESKTOP_TOOLS, ManageDesktopControlTool
from .desktop_semantic_tools import DESKTOP_SEMANTIC_TOOL_HANDLERS, SEMANTIC_TOOLS
from .exec_tools import InstallDependenciesTool, ManageScriptsTool
from .browser_tools import CaptureEvidenceTool, BrowserExtractTool
from .spreadsheet_tools import ManageSpreadsheetTool
from .git_tools import (
    GitInitTool, GitPublishTool, GitStatusTool, GitLogTool, GitDiffTool,
    GitBranchTool, GitCheckoutTool, GitCommitTool,
    GitMergeTool, GitDeleteBranchTool,
    GitPushTool, GitPullTool, GitFetchTool,
)
from .board_tools import (
    BoardListTool, BoardReadyTool, BoardGetTool,
    BoardCreateTool, BoardUpdateTool, BoardCommentTool,
    BoardLinkTool, BoardClaimTool,
)
from .requirement_tools import (
    ReqListTool, ReqGetTool, ReqMatrixTool,
    ReqProposeTool, ReqLinkTool,
)
from .project_concepts_tools import (
    ConceptsUnderstandTool, ConceptGetTool, ConceptsRootsTool,
    ConceptUpsertTool, ConceptLinkTool, ConceptRemoveTool,
)
from .design_canvas_tools import DesignCanvasTool
from .alternatives_tools import (
    AltStartTool, AltCompareTool, AltApplyTool,
)
from .code_mode_tool import RunCodeTool
from .fanout_tools import (
    FanoutRunTool, FanoutStatusTool, FanoutResultsTool, FanoutApplyTool,
)
from .context_overflow_tool import ReadOverflowTool
from .artifact_read_tool import ReadArtifactTool, ArtifactSearchTool
from .reach_tools import ReachReadTool, ReachSearchTool, ReachDoctorTool
from .code_graph_tools import (
    CodeGraphIndexTool, CodeGraphSearchTool, CodeGraphTraceTool,
    CodeGraphChangesTool, CodeGraphImpactTool, CodeGraphArchitectureTool,
    CodeGraphSnippetTool, CodeGraphCochangesTool, CodeGraphRiskTool,
)
from .structural_search_tools import StructuralSearchTool, StructuralRewriteTool
from .doc_claims_tool import DocClaimsCheckTool
from .pdf_ops_tool import PdfOpsTool
from .pdf_tree_tool import PdfOutlineTool, PdfReadSectionTool, PdfFindSectionTool
from .goal_tools import GoalDefineTool, GoalStatusTool, GoalEvaluateTool, GoalEvidenceTool
from .plan_tools import PlanStatusTool, PlanTaskTool, PlanDoneTool, PlanSkipTool, PlanNextTool

TOOL_HANDLERS = {
    "bash": BashTool().execute,
    "python": PythonTool().execute,
    "powershell": PowerShellTool().execute,
    "web_search": WebSearchTool().execute,
    "web_fetch": WebFetchTool().execute,
    "read_file": ReadFileTool().execute,
    "inspect_media": InspectMediaTool().execute,
    "plan_media_transform": MediaTransformTool(preview=True).execute,
    "transform_media": MediaTransformTool().execute,
    "write_file": WriteFileTool().execute,
    "edit_file": EditFileTool().execute,
    "apply_patch": ApplyPatchTool().execute,
    "todowrite": TodoWriteTool().execute,
    "delegate_agents": DelegateAgentsTool().execute,
    "ls": LsTool().execute,
    # A15: re-acquire a tool body the mid-turn compaction spilled to disk.
    "read_overflow": ReadOverflowTool().execute,
    # A12: read back an offloaded (oversized) tool result by range or query.
    "read_artifact": ReadArtifactTool().execute,
    # A12 follow-up: BM25 full-text search over offloaded tool results.
    "artifact_search": ArtifactSearchTool().execute,
    "glob": GlobTool().execute,
    "grep": GrepTool().execute,
    "find_symbol": FindSymbolTool().execute,
    "callers": CallersTool().execute,
    "tests_for": TestsForTool().execute,
    "rename_symbol": RenameSymbolTool().execute,
    "install_dependencies": InstallDependenciesTool().execute,
    "manage_scripts": ManageScriptsTool().execute,
    "manage_desktop_control": ManageDesktopControlTool().execute,
    "capture_evidence": CaptureEvidenceTool().execute,
    "browser_extract": BrowserExtractTool().execute,
    "manage_spreadsheet": ManageSpreadsheetTool().execute,
    "create_document": CreateDocumentTool().execute,
    "update_document": UpdateDocumentTool().execute,
    "edit_document": EditDocumentTool().execute,
    "suggest_document": SuggestDocumentTool().execute,
    "manage_documents": ManageDocumentTool().execute,
    "get_workspace": GetWorkspaceTool().execute,
    "ask_user": AskUserTool().execute,
    "update_plan": UpdatePlanTool().execute,
    "lookup_tools": LookupToolsTool().execute,
    "chat_with_model": ChatWithModelTool().execute,
    "ask_teacher": AskTeacherTool().execute,
    "list_models": ListModelsTool().execute,
    "manage_bg_jobs": ManageBgJobsTool().execute,
    "create_session": CreateSessionTool().execute,
    "list_sessions": ListSessionsTool().execute,
    "send_to_session": SendToSessionTool().execute,
    "manage_session": ManageSessionTool().execute,
    # Git tools (Lote 87, OBJ-4): "gestionarlo en vivo con el modelo" --
    # thin executors over src.git_panel, same runner the Source Control
    # panel uses. See src/agent_tools/git_tools.py for workspace confinement
    # and the agent git policy gate.
    "git_init": GitInitTool().execute,
    "git_publish": GitPublishTool().execute,
    "git_status": GitStatusTool().execute,
    "git_log": GitLogTool().execute,
    "git_diff": GitDiffTool().execute,
    "git_branch": GitBranchTool().execute,
    "git_checkout": GitCheckoutTool().execute,
    "git_commit": GitCommitTool().execute,
    "git_merge": GitMergeTool().execute,
    "git_delete_branch": GitDeleteBranchTool().execute,
    "git_push": GitPushTool().execute,
    "git_pull": GitPullTool().execute,
    "git_fetch": GitFetchTool().execute,
    # Project board tools (Lote 92, OBJ-6): "the project's task list" --
    # thin executors over src.project_board. See src/agent_tools/board_tools.py.
    "board_list": BoardListTool().execute,
    "board_ready": BoardReadyTool().execute,
    "board_get": BoardGetTool().execute,
    "board_create": BoardCreateTool().execute,
    "board_update": BoardUpdateTool().execute,
    "board_comment": BoardCommentTool().execute,
    "board_link": BoardLinkTool().execute,
    "board_claim": BoardClaimTool().execute,
    # Versioned requirements tools (ADP-18/19/20): thin executors over
    # src.requirements. See src/agent_tools/requirement_tools.py.
    "req_list": ReqListTool().execute,
    "req_get": ReqGetTool().execute,
    "req_matrix": ReqMatrixTool().execute,
    "req_propose": ReqProposeTool().execute,
    "req_link": ReqLinkTool().execute,
    # Project concepts: the agent's own persistent, per-project architecture
    # graph. See src/agent_tools/project_concepts_tools.py.
    "concepts_understand": ConceptsUnderstandTool().execute,
    "concept_get": ConceptGetTool().execute,
    "concepts_roots": ConceptsRootsTool().execute,
    "concept_upsert": ConceptUpsertTool().execute,
    "concept_link": ConceptLinkTool().execute,
    "concept_remove": ConceptRemoveTool().execute,
    # OBJ-30: declare the design before writing code, and file it in the same
    # graph. See src/agent_tools/design_canvas_tools.py.
    "design_canvas": DesignCanvasTool().execute,
    # Isolated, comparable alternatives (CMP-13, W2-G): thin executors over
    # src.alternatives. See src/agent_tools/alternatives_tools.py.
    "alt_start": AltStartTool().execute,
    "alt_compare": AltCompareTool().execute,
    "alt_apply": AltApplyTool().execute,
    # Code Mode (T6, A10/A11): compose several tool calls in one isolated
    # subprocess round instead of one model round trip per call. See
    # src/code_mode/ and src/agent_tools/code_mode_tool.py.
    "run_code": RunCodeTool().execute,
    # Reach (R1): "eyes on the internet" channels with real, ordered backend
    # fallback. See src/reach/ and src/agent_tools/reach_tools.py.
    "reach_read": ReachReadTool().execute,
    "reach_search": ReachSearchTool().execute,
    "reach_doctor": ReachDoctorTool().execute,
    # Code graph (R2, Reach wave): architecture/tracing queries over
    # src.context_engine.code_index's resolved graph. See src/code_graph/.
    "code_graph_index": CodeGraphIndexTool().execute,
    "code_graph_search": CodeGraphSearchTool().execute,
    "code_graph_trace": CodeGraphTraceTool().execute,
    "code_graph_changes": CodeGraphChangesTool().execute,
    "code_graph_impact": CodeGraphImpactTool().execute,
    "code_graph_risk": CodeGraphRiskTool().execute,
    "code_graph_architecture": CodeGraphArchitectureTool().execute,
    "code_graph_snippet": CodeGraphSnippetTool().execute,
    "code_graph_cochanges": CodeGraphCochangesTool().execute,
    # Structural (AST-pattern) search/rewrite via ast-grep -- shape queries
    # text grep and the code graph can't express. See src/structural_search.py.
    "structural_search": StructuralSearchTool().execute,
    "structural_rewrite": StructuralRewriteTool().execute,
    # Doc-claim drift checker: grounds backticked doc claims (paths, symbols,
    # settings keys, routes, tool names) in code evidence and flags stale
    # sections. See src/doc_claims.py.
    "doc_claims_check": DocClaimsCheckTool().execute,
    # R3 (Reach wave): fan one prompt across N candidate models/endpoints,
    # each isolated via src.alternatives, ranked by src.fanout.score. See
    # src/fanout/ and src/agent_tools/fanout_tools.py.
    "fanout_run": FanoutRunTool().execute,
    "fanout_status": FanoutStatusTool().execute,
    "fanout_results": FanoutResultsTool().execute,
    "fanout_apply": FanoutApplyTool().execute,
    # PDF operations (R4, Reach wave): merge/split/rotate/compress/watermark/
    # etc. over src.pdf_ops. See src/agent_tools/pdf_ops_tool.py.
    "pdf_ops": PdfOpsTool().execute,
    # Structural PDF navigation (tree-index RAG, src/pdf_tree.py): a
    # deterministic outline/bookmark or heading tree with EXACT page ranges,
    # read one section at a time instead of chunking by embedding similarity.
    # See src/agent_tools/pdf_tree_tool.py.
    "pdf_outline": PdfOutlineTool().execute,
    "pdf_read_section": PdfReadSectionTool().execute,
    "pdf_find_section": PdfFindSectionTool().execute,
    # Goal with completion by evidence (WP27, Creator): thin executors over
    # src.creator.goal. Only goal_evaluate can ever move a goal to done.
    "goal_define": GoalDefineTool().execute,
    "goal_status": GoalStatusTool().execute,
    "goal_evaluate": GoalEvaluateTool().execute,
    "goal_evidence": GoalEvidenceTool().execute,
    # P1: plan tracker tools (src/plan_tracker.py) — a persisted, per-project
    # plan attachment (=== File/ZIP: ... === parsed once) instead of
    # reinjected in full every chat.
    "plan_status": PlanStatusTool().execute,
    "plan_task": PlanTaskTool().execute,
    "plan_done": PlanDoneTool().execute,
    "plan_skip": PlanSkipTool().execute,
    "plan_next": PlanNextTool().execute,
}
# Config/integration admin tools (manage_endpoints/mcp/webhooks/tokens/settings).
TOOL_HANDLERS.update(ADMIN_TOOL_HANDLERS)
# Desktop control (screenshot / windows / mouse / keyboard) — FAUSTUS.
TOOL_HANDLERS.update(DESKTOP_TOOL_HANDLERS)
# ADP-08/09 semantic desktop tools (snapshot/find/act by control identity).
TOOL_HANDLERS.update(DESKTOP_SEMANTIC_TOOL_HANDLERS)

# ---------------------------------------------------------------------------
# Constants (re-exported for backward compatibility — single source of truth
# is src.constants; always prefer importing from there for new code)
# ---------------------------------------------------------------------------
MAX_AGENT_ROUNDS = 50
SHELL_TIMEOUT = 60
PYTHON_TIMEOUT = 30

# Tool types that trigger execution
TOOL_TAGS = {"bash", "python", "powershell", "web_search", "web_fetch", "read_file", "inspect_media", "write_file", "edit_file",
             "plan_media_transform", "transform_media",
             "apply_patch", "todowrite", "delegate_agents",
             "grep", "glob", "ls", "find_symbol", "callers", "tests_for", "rename_symbol",
             # Lote 54 — cableado de tools sobre librerías ya existentes:
             # EXEC-05/06 (src/tool_execution.py), DESK-01
             # (src/desktop_control_session.py), WEB-05/06
             # (src/browser_evidence.py / src/browser_extraction.py) y ART-04
             # (src/spreadsheet.py). Sin estas entradas, function_call_to_tool_block
             # (src/tool_schemas.py) rechaza la llamada nativa como "Unknown
             # function call" antes de llegar al TOOL_HANDLERS de arriba.
             "install_dependencies", "manage_scripts", "manage_desktop_control",
             "capture_evidence", "browser_extract", "manage_spreadsheet",
             "get_workspace", "manage_bg_jobs",
             "create_document", "update_document", "edit_document",
             "search_chats", "search_project_chats", "project_context",
             # Mutating half of the project's context links: attach/detach a
             # source. Separate tag from the read-only `project_context` so the
             # fence regex, dispatch and the non-admin blocklist all see it.
             "manage_project_context",
             "project_objectives", "manage_teach_mode", "capability_health", "branch_futures",
             "memory_rules", "expert_review", "verify_claim", "review_candidature_mail", "whatsapp_read", "whatsapp_send", "whatsapp_react",
             "chat_with_model", "create_session", "list_sessions",
             "send_to_session",
             "pipeline",
             "manage_session", "manage_memory", "list_models",
             "ui_control", "generate_image", "ask_user", "update_plan",
             "lookup_tools",
             # A15 / A12: re-acquire what compaction or offload took out of context.
             "read_overflow", "read_artifact",
             # A12 follow-up: full-text search over offloaded tool results
             # (src/offload_search.py) instead of paging read_artifact blindly.
             "artifact_search",
             "manage_tasks", "api_call", "ask_teacher", "manage_skills",
             "suggest_document",
             "manage_endpoints", "manage_mcp", "manage_webhooks",
             "manage_tokens", "manage_documents", "manage_settings",
             "manage_notes", "manage_calendar",
             # Code Mode (T6, A10/A11): src/agent_tools/code_mode_tool.py.
             "run_code",
             "resolve_contact", "manage_contact",
             # Email tool names come from BUILTIN_EMAIL_TOOLS (unioned below)
             # so the fence regex, dispatch, and non-admin blocklist all cover
             # the same set.
             # Cookbook tools (LLM serving + downloads). Without these
             # entries, native function calls to e.g. list_served_models
             # are rejected as "Unknown function call" before reaching
             # the dispatcher — silent failure for the whole cookbook
             # surface.
             "download_model", "serve_model",
             "list_served_models", "stop_served_model",
             "list_downloads", "cancel_download",
             "search_hf_models", "list_cached_models",
             "list_serve_presets", "serve_preset", "adopt_served_model",
             "list_cookbook_servers",
             # Other tools the agent reaches for that were also missing.
             "edit_image", "trigger_research", "manage_research",
             # Goal with completion by evidence (WP27, Creator):
             # src/agent_tools/goal_tools.py.
             "goal_define", "goal_status", "goal_evaluate", "goal_evidence",
             # P1: plan tracker (src/plan_tracker.py, src/agent_tools/plan_tools.py).
             "plan_status", "plan_task", "plan_done", "plan_skip", "plan_next",
             # Generic loopback to any UI-button endpoint (cookbook,
             # gallery, email folders, etc.) — agent uses this when
             # there's no named tool wrapper for the action.
             "app_api",
             # Git tools (Lote 87, OBJ-4; git_merge/git_delete_branch Lote 89) —
             # src/agent_tools/git_tools.py.
             "git_init", "git_publish", "git_status", "git_log", "git_diff",
             "git_branch", "git_checkout", "git_commit",
             "git_merge", "git_delete_branch",
             "git_push", "git_pull", "git_fetch",
             # Project board tools (Lote 92, OBJ-6) -- src/agent_tools/board_tools.py.
             "board_list", "board_ready", "board_get",
             "board_create", "board_update", "board_comment",
             "board_link", "board_claim",
             # Versioned requirements tools (ADP-18/19/20) --
             # src/agent_tools/requirement_tools.py.
             "req_list", "req_get", "req_matrix",
             "req_propose", "req_link",
             # Project concepts -- src/agent_tools/project_concepts_tools.py.
             "concepts_understand", "concept_get", "concepts_roots",
             "concept_upsert", "concept_link", "concept_remove",
             # Design canvas (OBJ-30) -- src/agent_tools/design_canvas_tools.py.
             "design_canvas",
             # Isolated, comparable alternatives (CMP-13, W2-G) --
             # src/agent_tools/alternatives_tools.py.
             "alt_start", "alt_compare", "alt_apply",
             # Reach (R1) -- src/agent_tools/reach_tools.py.
             "reach_read", "reach_search", "reach_doctor",
             # Code graph (R2, Reach wave) -- src/agent_tools/code_graph_tools.py.
             "code_graph_index", "code_graph_search", "code_graph_trace",
             "code_graph_changes", "code_graph_impact", "code_graph_architecture",
             "code_graph_snippet", "code_graph_cochanges", "code_graph_risk",
             # Structural search/rewrite -- src/agent_tools/structural_search_tools.py.
             "structural_search", "structural_rewrite",
             # Doc-claim drift checker -- src/agent_tools/doc_claims_tool.py.
             "doc_claims_check",
             # R3 (Reach wave): fan-out -- src/agent_tools/fanout_tools.py.
             "fanout_run", "fanout_status", "fanout_results", "fanout_apply",
             # PDF operations (R4, Reach wave) -- src/agent_tools/pdf_ops_tool.py.
             "pdf_ops",
             # Structural PDF navigation -- src/agent_tools/pdf_tree_tool.py.
             "pdf_outline", "pdf_read_section", "pdf_find_section"} | BUILTIN_EMAIL_TOOLS | DESKTOP_TOOLS | SEMANTIC_TOOLS

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])

# ---------------------------------------------------------------------------
# Re-exports from sub-modules
# ---------------------------------------------------------------------------

# Parsing
from src.tool_parsing import (  # noqa: E402, F401
    parse_tool_blocks,
    strip_tool_blocks,
    _TOOL_NAME_MAP,
    _TOOL_BLOCK_RE,
    _TOOL_CALL_RE,
    _XML_TOOL_CALL_RE,
    _XML_INVOKE_RE,
    _XML_PARAM_RE,
)

# Schemas
from src.tool_schemas import (  # noqa: E402, F401
    FUNCTION_TOOL_SCHEMAS,
    function_call_to_tool_block,
)

# Execution
from src.tool_execution import (  # noqa: E402, F401
    execute_tool_block,
    format_tool_result,
)

# Document functions
from .document_tools import (
    set_active_document, 
    set_active_model
)

# Implementations
from src.tool_implementations import (  # noqa: E402, F401
    do_search_chats,
    do_manage_skills,
    do_manage_tasks,
    do_api_call,
)
