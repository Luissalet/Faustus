"""
tool_schemas.py

OpenAI-compatible function tool schemas and the converter that turns
native function calls back into ToolBlocks for the execution pipeline.

Extracted from agent_tools.py to keep schema definitions separate from
tool parsing / execution logic.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.agent_tools import ToolBlock, TOOL_TAGS
from src.tool_parsing import _TOOL_NAME_MAP
from src.tool_security import BUILTIN_EMAIL_TOOLS

logger = logging.getLogger(__name__)


_REQUIRED_NATIVE_TOOL_ARGS = {
    "web_search": ("query", "queries"),
    "web_fetch": ("url",),
    "read_file": ("path",),
    "inspect_media": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "apply_patch": ("patch_text", "patchText", "patch"),
}

# ---------------------------------------------------------------------------
# OpenAI-compatible function tool schemas
# ---------------------------------------------------------------------------
FUNCTION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "plan_media_transform",
            "description": "Read-only preflight of a fixed media recipe: convert/resize single-frame images to PNG/JPEG/WebP or extract first audio track to WAV/MP3 (stereo 48 kHz, max 10 minutes). Measures input, reports losses and engine requirements. Does not write, install or run a conversion. Original always preserved. path must be a new filename in an existing workspace folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Local authorized source file"},
                    "path": {"type": "string", "description": "Proposed NEW output filename with matching extension"},
                    "format": {"type": "string", "enum": ["png", "jpeg", "webp", "wav", "mp3"]},
                    "max_width": {"type": "integer", "minimum": 1, "maximum": 8192},
                    "max_height": {"type": "integer", "minimum": 1, "maximum": 8192},
                    "quality": {"type": "integer", "minimum": 1, "maximum": 100, "description": "JPEG/WebP only, default 90; not a filesize target"},
                    "background": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$", "description": "JPEG only: explicit background for images with alpha"}
                },
                "required": ["source", "path", "format"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "transform_media",
            "description": "Execute a fixed local media recipe to a NEW workspace file; never overwrite original or existing output. Images: PNG/JPEG/WebP, max 16MP, preserves aspect/no upscale, rejects animation/multipage, strips metadata; JPEG alpha needs explicit background. Audio/video: extract first audio track to WAV 16-bit or MP3 192k, stereo 48kHz, up to 10 minutes, FFmpeg required. Max input 256MiB/output 128MiB, bounded runtime, cancellable with progress. Returns validated output and hashes; no installs or AI calls. Preflight with plan_media_transform. Not video encoding, subtitles, color-managed print or target-filesize compression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Local authorized source file"},
                    "path": {"type": "string", "description": "NEW output filename in an existing authorized folder; matching extension"},
                    "format": {"type": "string", "enum": ["png", "jpeg", "webp", "wav", "mp3"]},
                    "max_width": {"type": "integer", "minimum": 1, "maximum": 8192},
                    "max_height": {"type": "integer", "minimum": 1, "maximum": 8192},
                    "quality": {"type": "integer", "minimum": 1, "maximum": 100, "description": "JPEG/WebP only, default 90"},
                    "background": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$", "description": "JPEG only: explicit background for images with alpha"}
                },
                "required": ["source", "path", "format"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_media",
            "description": "Inspect a local image, audio or video before editing/converting it. Returns measured dimensions, display orientation, format, duration and stream metadata when available. Read-only, workspace-confined; no model call, network fetch, transcription or generation. Images and PCM WAV work locally; compressed audio/video require FFprobe. Missing metadata is not guessed.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Local media file path inside the allowed workspace; not a URL or playlist"}},
                "required": ["path"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command (full access). Prefer a dedicated tool whenever one fits the job (reading, writing, editing, searching, or listing files); use bash only for what no dedicated tool covers (installs, git, builds, running programs, system info). Do NOT create or edit files via bash redirects/heredocs/sed -- use the dedicated file tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "python",
            "description": "Execute Python code to compute a result or test something. Prefer a dedicated tool whenever one fits the job (reading, writing, or searching files); use python only for computation, data processing, or scripting no dedicated tool covers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Quick single web lookup for a fact or current event mid-task. NOT for 'research X' / 'do research on X' — those are deep-research jobs; use trigger_research instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "time_filter": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Optional freshness filter for news/latest/today queries"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the text content of a specific URL the user names (e.g. 'check example.com', 'what's on this page <url>'). Use when you already have a concrete URL/domain. NOT for open-ended searches (use web_search) or 'research X' jobs (use trigger_research). Downloads are size-budgeted; a '[partial content: ...]' notice in the result means the body was cut short and you can re-call with full=true for the rest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL or domain to fetch (http/https; a bare domain like example.com is fine)"},
                    "full": {"type": "boolean", "description": "Raise the download budget to the hard cap for large pages/files. Use only after a result reported partial content."}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from disk. Optionally read a line range with offset/limit for large files. The result carries a `revision` (the file's current content hash) — pass it back as `base_revision` to write_file/edit_file/apply_patch so the edit is refused instead of silently applied if the file changed since this read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
                    "offset": {"type": "integer", "description": "1-based line to start reading from (optional)"},
                    "limit": {"type": "integer", "description": "Max number of lines to read from offset (optional)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regular expression across a directory tree (uses ripgrep when available, respecting .gitignore). Returns file:line:match. PREFER this over `bash grep/rg` for code search — confined to the allowed roots, structured output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (optional; defaults to the project root)"},
                    "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py' (optional)"},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive match (optional)"},
                    "max_results": {"type": "integer", "description": "Max matches to return (optional)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern (recursive), newest first. e.g. '**/*.py'. PREFER this over `bash find/ls` for locating files — confined to the allowed roots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.ts' or 'src/**/test_*.py'"},
                    "path": {"type": "string", "description": "Base directory (optional; defaults to the project root)"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List the entries of a directory (folders first, then files with sizes). PREFER this over `bash ls` — confined to the allowed roots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (optional; defaults to the project root)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": "Find where a symbol (class/function/method/constant) is DEFINED, using the incremental code index (IDX-02) instead of reading files one at a time. Respects .gitignore/.faustusignore and skips vendor/build/binary files, so it works on a large repository without loading it whole. PREFER this over grep for 'where is X defined' questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name: a bare name ('refresh') or a dotted qualname ('Session.refresh_oauth_token')"},
                    "path": {"type": "string", "description": "Workspace/project root to search (optional; defaults to the project root)"},
                    "project_id": {"type": "string", "description": "Optional project scope, when more than one project shares a workspace"},
                    "kind": {"type": "string", "description": "Optional: restrict to one kind (class/function/method/constant)"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "callers",
            "description": "Find lexical callers of a symbol - every place its bare name appears immediately followed by '(' - with file and line, using the incremental code index (IDX-02/IDX-03). PREFER this over grep for 'who calls X' questions on an indexed workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name to find callers of"},
                    "path": {"type": "string", "description": "Workspace/project root to search (optional; defaults to the project root)"},
                    "project_id": {"type": "string", "description": "Optional project scope"},
                    "limit": {"type": "integer", "description": "Max callers to return (optional, default 200)"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tests_for",
            "description": "Find candidate test files for a symbol or a file path, by convention (test_<module>, tests/**/test_* that names the module in its own filename, or mentions the symbol) using the incremental code index (IDX-02/IDX-03/QA-02).",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol_or_path": {"type": "string", "description": "A symbol name or a file path to find tests for"},
                    "path": {"type": "string", "description": "Workspace/project root to search (optional; defaults to the project root)"},
                    "project_id": {"type": "string", "description": "Optional project scope"}
                },
                "required": ["symbol_or_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_workspace",
            "description": "Return the absolute path of the active workspace folder the user is working in. File tools are confined to it; the shell starts there but is not sandboxed. Call this first when the user refers to 'the project'/'the code'/'this folder' without a path, instead of asking them. Takes no arguments.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write/save a file to disk",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "File content to write"},
                    "base_revision": {"type": "string", "description": "Optional: the `revision` a prior read_file of this exact path returned. If the file changed since then, the write is refused instead of overwriting the newer content."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Edit a file ON DISK by exact string replacement (home folder, project files, any real path like ~/sweden.txt or /path/to/file). This is the right tool for files on disk — NOT edit_document (that's for editor-panel documents). PREFER this over bash (sed/echo) — it shows a diff. old_string must match the file exactly and be unique (or set replace_all). Use write_file to create a new file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact text to replace (must match the file, including indentation)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match"},
                    "base_revision": {"type": "string", "description": "Optional: the `revision` a prior read_file of this exact path returned. If the file changed since then, the edit is refused instead of applying on top of the newer content."}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a multi-file source-code patch to disk. Use for real project files in the workspace when several edits belong together. Patch must use *** Begin Patch / *** End Patch with Add File, Update File, or Delete File sections. Prefer this over bash redirects/heredocs/sed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patch_text": {
                        "type": "string",
                        "description": "Patch text beginning with *** Begin Patch and ending with *** End Patch"
                    },
                    "base_revision": {"type": "string", "description": "Optional: the `revision` a prior read_file returned for the file(s) this patch updates or deletes. Anchors every Update/Delete section in the patch — if any of those files changed since, the whole patch is refused before anything is touched. Not meaningful for a pure Add File."}
                },
                "required": ["patch_text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todowrite",
            "description": "Create and maintain a structured task list for the current coding session. Use during multi-step implementation/debug/refactor work and keep statuses current.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Current task list. Only one item should be in_progress.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "description": "Task description"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                                "priority": {"type": "string", "enum": ["low", "medium", "high"]}
                            },
                            "required": ["content", "status"]
                        }
                    }
                },
                "required": ["todos"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_agents",
            "description": "Split a big job into independent sub-tasks and run each with its own sub-agent (same workspace, same tools, same reliability checks), in parallel. Use for work that decomposes into 2-4 separate pieces (e.g. backend route + frontend component + tests). Returns an evidence-based report per worker: files really changed, tool calls, failures, child chat id. Do not use for single small edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "2-4 independent sub-tasks. Each instruction must be self-contained (mention the files/areas involved).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Short label shown in the UI"},
                                "team_member": {"type": "string", "description": "Configured chat team member ID. Required when the user enabled a chat team; routes and restrictions come from that member."},
                                "instruction": {"type": "string", "description": "Complete instruction for the worker"}
                            },
                            "required": ["instruction"]
                        }
                    },
                    "context": {"type": "string", "description": "Optional shared context every worker should know (conventions, constraints)"},
                    "parallel": {"type": "boolean", "description": "Run workers concurrently (default true)"},
                    "max_rounds": {"type": "integer", "description": "Tool rounds allowed per worker (default 14)"}
                },
                "required": ["tasks"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "Create a new document in the editor panel. Use this when the user asks to write, create, build, make, or generate code, scripts, programs, games, apps, or any long-form or structured content that is more than a short paragraph, AND there is no already-open document/email draft that the request refers to. If an email compose draft is open, edit that draft instead of creating another document. NEVER put large generated content directly in chat — use this tool instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title"},
                    "language": {"type": "string", "description": "Programming language or format (e.g. python, javascript, markdown, text)"},
                    "content": {"type": "string", "description": "The document content"}
                },
                "required": ["title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_document",
            "description": "Edit a document OPEN IN THE EDITOR PANEL (created via create_document) — NOT a file on disk. For files on disk (home folder, project files, anything with a path like ~/x.txt or /path/to/file) use edit_file instead. Targeted find-and-replace with multiple FIND/REPLACE pairs per call; use for any edit smaller than a full rewrite. Do NOT send the whole file back via update_document for small edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "List of find/replace edits (first match only per edit)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "Exact text to find in the document"},
                                "replace": {"type": "string", "description": "Text to replace it with"}
                            },
                            "required": ["find", "replace"]
                        }
                    }
                },
                "required": ["edits"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_document",
            "description": "Suggest improvements to the active document WITHOUT editing it. Creates inline comment bubbles the user can accept or reject. Use when the user asks for suggestions, review, improvements, or feedback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "suggestions": {
                        "type": "array",
                        "description": "List of suggested changes with reasons",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string", "description": "Exact text in the document to suggest changing"},
                                "replace": {"type": "string", "description": "Suggested replacement text"},
                                "reason": {"type": "string", "description": "Brief explanation of why this change helps"}
                            },
                            "required": ["find", "replace", "reason"]
                        }
                    }
                },
                "required": ["suggestions"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_document",
            "description": "Replace the ENTIRE active document. ONLY use for genuine full rewrites (>50% of lines changed). For any smaller change, use edit_document — echoing back the whole file for small edits is wasteful.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Complete new document content"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_chats",
            "description": "Search the user's past session transcripts by keyword. Use when the user asks about previous chats, past conversations, or when direct transcript evidence is better than persistent memory. Returns matching sessions with clickable links and nearby context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword(s) to find in past conversations"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_project_chats",
            "description": "Search only earlier chat transcripts from the current project. Use for prior decisions, attempts, and discussions that may not be in project memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to find in this project's earlier chats"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_context",
            "description": "Inspect the files and folders attached to the current project. Use list to see roots or a folder, read for a text file, and search across attached roots. Normal file tools may modify these same roots.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "read", "search"]},
                    "item_id": {"type": "string", "description": "Root id from list; optional for listing all roots or searching all roots"},
                    "path": {"type": "string", "description": "Relative path inside a folder root"},
                    "query": {"type": "string", "description": "Text to search for"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "line_count": {"type": "integer", "minimum": 1, "maximum": 500}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_project_context",
            "description": "manage_project_context — Attach, detach or configure a source in the current project's durable context. Use it when the user asks to add/save/link a document, artifact, generated media, file or folder to this project. The project is resolved from the current chat; never invent a project ID. Args JSON: {\"action\":\"attach|detach|update|refresh|inspect\",\"source\":{\"kind\":\"document|artifact|file|folder|active_document\",\"id\":\"...\",\"path\":\"...\"},\"link_id\":\"...\",\"retrieval_policy\":\"auto|pinned_summary|on_demand|disabled\",\"version_policy\":\"latest|pinned|snapshot\",\"pinned_version\":null,\"role\":\"reference\",\"label\":\"...\",\"tags\":[]}",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["attach", "detach", "update", "refresh", "inspect", "list"]},
                    "source": {
                        "type": "object",
                        "description": "What to attach. Use kind 'active_document' for the document just created or edited in this chat.",
                        "properties": {
                            "kind": {"type": "string",
                                     "enum": ["document", "artifact", "file", "folder",
                                              "gallery_image", "active_document"]},
                            "id": {"type": "string", "description": "Row id for document/artifact/gallery_image"},
                            "path": {"type": "string", "description": "Absolute path for file/folder"}
                        }
                    },
                    "link_id": {"type": "string", "description": "Existing link id (ctx_...) for detach/update/refresh/inspect"},
                    "label": {"type": "string", "description": "Human-readable name for the link"},
                    "role": {"type": "string",
                             "enum": ["requirements", "reference", "decision", "style_reference",
                                      "example", "dataset", "specification", "output", "archive"]},
                    "retrieval_policy": {"type": "string",
                                         "enum": ["auto", "pinned_summary", "on_demand", "disabled"]},
                    "version_policy": {"type": "string", "enum": ["latest", "pinned", "snapshot"]},
                    "pinned_version": {"type": "integer", "minimum": 1},
                    "access_mode": {"type": "string", "enum": ["read_only", "work_root"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "enabled": {"type": "boolean", "description": "For update: suspend or resume a link without removing it"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "project_objectives",
            "description": "Read or update the current project's objectives dashboard. 'list' returns the objectives with dependencies and impact scores; 'apply' submits typed deltas — never rewrite the whole list. Each delta: {\"op\":\"ADD|EDIT|KILL\",\"id\":\"OBJ-1\" (EDIT/KILL),\"title\",\"status\":\"open|in_progress|blocked|done|dropped\",\"priority\":1-4 (1 highest),\"notes\",\"deps\":[\"OBJ-2\"],\"rationale\",\"base_updated_at\"}. Statuses must reflect what actually changed on disk, not intentions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "apply"]},
                    "deltas": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": ["ADD", "EDIT", "KILL"]},
                                "id": {"type": "string", "description": "Existing OBJ-N identifier for EDIT or KILL"},
                                "title": {"type": "string", "minLength": 1, "maxLength": 200,
                                          "description": "Required for ADD; the objective's readable title"},
                                "status": {"type": "string", "enum": ["open", "in_progress", "blocked", "done", "dropped"]},
                                "priority": {"type": "integer", "minimum": 1, "maximum": 4},
                                "notes": {"type": "string", "description": "Description or supporting details"},
                                "deps": {"type": "array", "items": {"type": "string"}},
                                "rationale": {"type": "string", "description": "Why this change is justified; required for KILL"},
                                "base_updated_at": {"type": "string", "description": "Timestamp read before EDIT, to detect concurrent changes"}
                            },
                            "required": ["op"],
                            "additionalProperties": False
                        },
                        "description": "Typed objective changes; not JSON Patch paths or nested value objects"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_teach_mode",
            "description": "Record a demonstration in this chat and compile it into a learned procedure. start begins automatic capture of subsequent semantic tool calls; stop ends capture; compile creates a candidate; simulate/validate advance it with evidence. Agents cannot approve or install their own procedure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["status", "start", "pause", "resume", "stop", "cancel", "compile", "simulate", "validate"]},
                    "title": {"type": "string"}, "intent": {"type": "string"},
                    "type": {"type": "string", "enum": ["tool_native", "workflow", "workspace", "computer_use", "hybrid"]},
                    "demonstration_id": {"type": "string"}, "procedure_id": {"type": "string"},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "target_refs": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "object"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "capability_health",
            "description": "Read or update Immune System health for a skill, workflow, tool, connector, model or learned procedure. Use report_failure when a capability actually fails so repeated failures can be deduplicated and contained.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "list", "register", "assess", "report_failure"]},
                    "asset_id": {"type": "string"}, "status": {"type": "string"},
                    "asset": {"type": "object"}, "assessment": {"type": "object"},
                    "failure": {"type": "object"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "branch_futures",
            "description": "Create and inspect equivalent isolated alternatives, submit their observed results and evaluate them. Branches never have real external effects. Selection and commit are human-only operations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "list", "create", "start_branch", "submit_result", "evaluate"]},
                    "future_id": {"type": "string"}, "branch_id": {"type": "string"},
                    "future": {"type": "object"}, "result": {"type": "object"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_rules",
            "description": "The learned-memory store: rules and facts that are SCORED by what happened after they were used, decay on their own, and are inverted into anti-patterns when they keep causing failures. 'add' records a new one (level 'procedural' for a rule you should follow, 'semantic' for a durable fact); 'search' returns the ones relevant to a query with their ids and scores; 'feedback' credits or blames one by id after you saw it help or hurt; 'list' shows what is stored. Add a rule only when a turn actually taught you something reusable — not to restate the request.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "search", "feedback", "list"]},
                    "text": {"type": "string", "description": "The rule or fact, for action 'add'"},
                    "level": {"type": "string", "enum": ["procedural", "semantic", "episodic", "working"],
                              "description": "'procedural' (default) for a rule to follow, 'semantic' for a durable fact"},
                    "category": {"type": "string", "description": "Optional free-text grouping, e.g. 'testing'"},
                    "query": {"type": "string", "description": "Search text for action 'search'"},
                    "id": {"type": "string", "description": "Item id for action 'feedback' (the [id8] prefix is enough)"},
                    "kind": {"type": "string", "enum": ["helpful", "harmful"],
                             "description": "Which way the feedback goes, for action 'feedback'"},
                    "reason": {"type": "string", "description": "Why, for action 'feedback'"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "expert_review",
            "description": "Have a specialist expert (a profile plus its OWN indexed corpus of books) review a passage of the user's text and return typed span deltas — never rewritten prose. 'review' runs the pass: each correction is {op EDIT/ADD/KILL, span, quote, replacement, rationale, rule, severity} and is either ANCHORED to the corpus (with the book and page it came from) or labelled \"model's opinion, not the corpus\"; a page is never invented. 'experts' shows a profile, 'bible' reads the project's story bible or checks a passage against it for continuity errors (the character whose eyes changed colour between chapters), 'apply' splices accepted deltas into the text, 'feedback' reports how many corrections were accepted or rejected so the expert's retrieval learns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["review", "experts", "bible", "apply", "feedback"]},
                    "slug": {"type": "string", "description": "Which expert, e.g. 'brenner_bot'"},
                    "text": {"type": "string",
                             "description": "The passage to review, apply deltas to, or check for continuity"},
                    "deltas": {"type": "array", "items": {"type": "object"},
                               "description": "For 'apply': the deltas from a review. For 'bible': typed ADD/EDIT/KILL bible edits with a rationale"},
                    "accept": {"type": "array", "items": {"type": "string"},
                               "description": "For 'apply': the delta ids to accept, e.g. [\"D1\",\"D3\"]. Omit to accept all"},
                    "max_chars": {"type": "integer", "minimum": 400, "maximum": 20000,
                                  "description": "Scene chunk size for long text (default 3000)"},
                    "accepted": {"type": "integer", "minimum": 0,
                                 "description": "For 'feedback': how many corrections the user accepted"},
                    "rejected": {"type": "integer", "minimum": 0,
                                 "description": "For 'feedback': how many the user rejected"},
                    "use_bible": {"type": "boolean",
                                  "description": "For 'review': feed the project's story bible and its continuity findings into the pass (default true)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "verify_claim",
            "description": "Check one claim against the source text it is supposed to come from, deterministically and without any model. Four layers, cheapest first: (1) the claim occurs verbatim; (2) it occurs after folding case, accents and punctuation; (3) enough of its content words occur; (4) every figure and every capitalised name in the claim occurs in the source — the layer that catches a fabricated number or an invented citation, and the only one that can settle a claim AGAINST you, naming what is missing in `unsupported_terms`. Passing layer 4 is NOT support: a paraphrase can carry the right names and still be false. Returns {supported, layer, confidence, why, unsupported_terms, label}; `layer: null` means no layer could settle it, which is 'not shown', not 'false'. THERE IS NO LAYER 5 HERE: the model-judgement rung needs a judge model and this tool is the deterministic ladder on purpose, so do not expect it to adjudicate meaning. Pass the source text you already have — nothing is fetched — and `url` only to record where that text came from.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string",
                              "description": "The single sentence to check, written as you would state it"},
                    "source": {"type": "string",
                               "description": "The text it must be supported by: the page, document or excerpt you already have in this turn"},
                    "url": {"type": "string",
                            "description": "Optional: where the source text came from. Recorded on the answer so the verdict can be cited; it is NOT fetched"}
                },
                "required": ["claim", "source"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chat_with_model",
            "description": "Send a message to another AI model and get its response. Use for getting a second opinion, delegating subtasks, or AI-to-AI communication.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Model name (e.g. 'qwen3-32b') or model@endpoint_name"},
                    "message": {"type": "string", "description": "The message to send to the model"}
                },
                "required": ["model", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_session",
            "description": "Create a new chat for ongoing conversations with a specific model. (The UI calls these 'chats'; 'session' is the internal term.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the new chat"},
                    "model": {"type": "string", "description": "Model name or model@endpoint_name"}
                },
                "required": ["name", "model"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sessions",
            "description": "List the user's chats (the UI calls them 'chats') as clickable markdown links. Use this to enumerate chats before opening, renaming, archiving, or deleting them. When replying to the user, preserve the returned [title](#session-id) links; do not strip them into plain text. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional keyword to filter chats by name"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_to_session",
            "description": "Send a new message to an existing live chat and get that chat model's response. Do not use this to retrieve, read, summarize, or inspect old chats; use search_chats or list_sessions for past chat evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "The id of the chat to send the message to"},
                    "message": {"type": "string", "description": "The message to send"}
                },
                "required": ["session_id", "message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pipeline",
            "description": "Run a multi-step AI pipeline where each model's output feeds the next. Example: Draft -> Critique -> Revise.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Pipeline steps in order",
                        "items": {
                            "type": "object",
                            "properties": {
                                "model": {"type": "string", "description": "Model name for this step"},
                                "instruction": {"type": "string", "description": "What this step should do"}
                            },
                            "required": ["model", "instruction"]
                        }
                    }
                },
                "required": ["steps"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_session",
            "description": "Manage a chat: rename, archive, unarchive, delete, mark important, truncate history, or fork it. (The UI calls these 'chats'; 'session' is the internal term.) For destructive actions like delete, call list_sessions first and pass the exact id returned there; never invent ids.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["rename", "archive", "unarchive", "delete", "important", "unimportant", "truncate", "fork"],
                               "description": "The action to perform"},
                    "session_id": {"type": "string", "description": "Exact target chat id from list_sessions, or 'current' for the active chat where supported"},
                    "value": {"type": "string", "description": "Action parameter: new name (rename), keep_count (truncate/fork)"}
                },
                "required": ["action", "session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_memory",
            "description": "Manage the user's memory system: list, add, edit, delete, or search memories. Memories persist across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "edit", "delete", "search"],
                               "description": "The action to perform"},
                    "text": {"type": "string", "description": "Memory text (for add/edit) or search query (for search)"},
                    "memory_id": {"type": "string", "description": "Memory ID (for edit/delete)"},
                    "category": {"type": "string", "enum": ["fact", "event", "contact", "preference"],
                                 "description": "Memory category (for add/list filter)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_models",
            "description": "List all available AI models across configured endpoints. Optionally filter by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "description": "Optional keyword to filter models"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ui_control",
            "description": "Control the user interface. Actions: toggle (turn tools on/off), open_panel (open a modal: documents/library, gallery, email, sessions, notes, memories/brain, skills, settings, cookbook), open_email_reply (open an email reply draft document; DOES NOT send. For 'write/draft a reply saying X', include body with the drafted reply), set_mode, switch_model, set_theme (built-in presets: dark, light, midnight, paper, cyberpunk, retrowave, forest, ocean, ume, copper, terminal, organs, lavender, gpt, claude, cute), create_theme (CREATE any custom theme with a name + colors object — pick distinctive, evocative hex colors that match the requested aesthetic, NOT generic defaults. The theme auto-applies after creation). When a user asks for ANY theme not in the built-in preset list, ALWAYS use create_theme.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["toggle", "open_panel", "open_email_reply", "set_mode", "switch_model", "set_theme", "create_theme", "get_toggles"],
                               "description": "The UI action. Use set_theme for presets, create_theme to build a custom theme with any hex colors"},
                    "name": {"type": "string", "description": "For toggle: web, bash, research, incognito, document_editor (aliases: shell, search, deepresearch, documents). For open_panel: documents, gallery, email, sessions, notes, brain/memories, skills, settings, cookbook. For open_email_reply: email UID. For set_theme: a preset theme name. For create_theme: the custom theme name."},
                    "value": {"type": "string", "description": "Value: on/off for toggle, agent/chat for set_mode, model name for switch_model, theme name for set_theme, or folder for open_email_reply"},
                    "uid": {"type": "string", "description": "Email UID for open_email_reply"},
                    "folder": {"type": "string", "description": "Email folder for open_email_reply (default INBOX)"},
                    "mode": {"type": "string", "description": "Reply draft mode for open_email_reply: reply, reply-all, or ai-reply"},
                    "body": {"type": "string", "description": "For open_email_reply: reply body to pre-fill. Required whenever the user told you what the reply should say. Opens a draft, does not send."},
                    "colors": {"type": "object", "description": "For create_theme: the theme colors",
                               "properties": {
                                   "bg": {"type": "string", "description": "Background color (hex, e.g. #1a1a2e)"},
                                   "fg": {"type": "string", "description": "Foreground/text color (hex)"},
                                   "panel": {"type": "string", "description": "Panel/sidebar background color (hex)"},
                                   "border": {"type": "string", "description": "Border/divider color (hex)"},
                                   "accent": {"type": "string", "description": "Accent color for buttons, brand, highlights (hex)"},
                                   "userBubbleBg": {"type": "string", "description": "User chat bubble background (hex, optional)"},
                                   "aiBubbleBg": {"type": "string", "description": "AI chat bubble background (hex, optional)"},
                                   "bubbleBorder": {"type": "string", "description": "Chat bubble border color (hex, optional)"},
                                   "sidebarBg": {"type": "string", "description": "Sidebar background override (hex, optional)"},
                                   "sectionAccent": {"type": "string", "description": "Section header accent color (hex, optional)"},
                                   "brandColor": {"type": "string", "description": "Brand/logo color (hex, optional)"},
                                   "inputBg": {"type": "string", "description": "Chat input background (hex, optional)"},
                                   "inputBorder": {"type": "string", "description": "Chat input border (hex, optional)"},
                                   "sendBtnBg": {"type": "string", "description": "Send button background (hex, optional)"},
                                   "sendBtnHover": {"type": "string", "description": "Send button hover color (hex, optional)"},
                                   "codeBg": {"type": "string", "description": "Code block background (hex, optional)"},
                                   "codeFg": {"type": "string", "description": "Code block text color (hex, optional)"},
                                   "toggleBg": {"type": "string", "description": "Toggle switch off background (hex, optional)"},
                                   "toggleActive": {"type": "string", "description": "Toggle switch on color (hex, optional)"},
                                   "accentPrimary": {"type": "string", "description": "Primary accent override (hex, optional)"},
                                   "accentError": {"type": "string", "description": "Error/danger color (hex, optional)"}
                               },
                               "required": ["bg", "fg", "panel", "border", "accent"]}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a multiple-choice question to get a decision or clarification when the task is genuinely ambiguous and the answer changes what you do next (e.g. pick between approaches, confirm an assumption, choose a target). The user sees clickable option buttons; calling this ENDS your turn and their selection arrives as your next message. Prefer sensible defaults over asking — only ask when you truly cannot proceed well without the user's input. Do NOT use it to confirm irreversible/destructive actions that have a dedicated confirmation flow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask. Be specific and self-contained."},
                    "options": {
                        "type": "array",
                        "description": "2-6 choices. Each is an object with a short `label` and an optional `description` explaining the trade-off.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Concise choice text the user clicks (1-5 words)."},
                                "description": {"type": "string", "description": "Optional one-line explanation of this choice."}
                            },
                            "required": ["label"]
                        }
                    },
                    "multi": {"type": "boolean", "description": "Set true ONLY when the question explicitly allows choosing more than one option. Otherwise omit it or set false. Default false."}
                },
                "required": ["question", "options"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "Write back to the ACTIVE PLAN: mark steps done or revise them. Use this while executing an approved plan — after you finish a step, call update_plan with the full checklist and that step marked `- [x]`; when the user asks to change the plan, call it with the revised checklist. The user's docked plan window updates live. Pass the COMPLETE checklist every time (not a diff), as EITHER `plan` (markdown) OR `steps` (structured) — never both empty. No effect if there is no active plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "description": "The full updated plan as a GitHub-style markdown checklist — one step per line, `- [ ]` for pending and `- [x]` for done. Always send the whole list. Alternative to `steps`."},
                    "steps": {
                        "type": "array",
                        "description": "Alternative to `plan`: the full updated checklist as structured steps (send the whole list every time, not a diff). Each item is either a plain title string, or an object with status/dependency/evidence detail.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Stable step id from a prior update_plan/plan_update; omit for a new step."},
                                "title": {"type": "string", "description": "The step's text."},
                                "status": {"type": "string", "enum": ["pending", "done", "blocked"]},
                                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Ids of steps this one is nested under / blocked by."},
                                "evidence_refs": {"type": "array", "items": {"type": "string"}, "description": "What proves this step is actually done (a test name, a file, a command)."},
                                "notes": {"type": "string"},
                                "verified": {"type": "boolean", "description": "Whether a done step's evidence has actually been checked, not just claimed."}
                            },
                            "required": ["title"]
                        }
                    },
                    "revision": {"type": "integer", "minimum": 1, "description": "Optional revision number for the whole plan, with `steps`."}
                },
                "required": []
            }
        }
    },
    # ── Desktop control (FAUSTUS): see the screen and drive it ──────────────
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": "Capture the screen of the computer Faustus runs on (the user's desktop) and SEE it: the image is attached to your context. Returns the screen size, the returned image size and the scale (the image is downscaled to at most agent_tool_image_max_px on its longest side). Coordinates you pass later to desktop_click / desktop_scroll are pixels of THIS image (they are mapped back to the screen for you). Take a fresh screenshot after every action that changes the screen. Fails clearly when there is no interactive desktop (locked/headless session).",
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer", "description": "Monitor index (0 = primary). Default 0."},
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x, y, width, height] in SCREEN pixels to capture only that area (e.g. a window rect from desktop_list_windows). Coordinates of the returned image are then relative to that region."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_list_windows",
            "description": "List the visible top-level windows on the user's desktop: title, screen rect [left, top, right, bottom] and which one is in the foreground. Use it to find a window title for desktop_focus_window or a rect for a desktop_screenshot region.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_focus_window",
            "description": "Bring the first visible window whose title contains `title` (case-insensitive substring) to the foreground so keyboard input reaches it. Requires user approval on every call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Substring of the window title, e.g. \"Notepad\" or \"Firefox\"."}
                },
                "required": ["title"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": "Click the mouse on the user's desktop. x,y are pixels of the LAST desktop_screenshot image (default coords=\"screenshot\"; they are mapped back to real screen pixels using that screenshot's scale and origin) — take a screenshot first, then click what you see. Use coords=\"screen\" to pass raw screen pixels. Requires user approval on every call. Take a new screenshot afterwards to verify the effect.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Horizontal position (pixels of the last screenshot image, or screen pixels with coords=\"screen\")."},
                    "y": {"type": "integer", "description": "Vertical position (same frame as x)."},
                    "button": {"type": "string", "enum": ["left", "right", "double", "middle"], "description": "Mouse button / double-click. Default left."},
                    "coords": {"type": "string", "enum": ["screenshot", "screen"], "description": "Coordinate frame. Default screenshot."}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": "Type text into whatever has keyboard focus on the user's desktop (unicode is fine; \\n presses Enter, \\t presses Tab). Click or focus the target field first. Requires user approval on every call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to type, up to 5000 characters."}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_key",
            "description": "Press a key or key combination on the user's desktop, e.g. \"enter\", \"ctrl+s\", \"alt+tab\", \"ctrl+shift+t\", \"win+d\", \"f5\", \"escape\". Modifiers: ctrl, alt, shift, win. Requires user approval on every call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "combo": {"type": "string", "description": "Keys joined with '+': modifiers first, then exactly one key (a named key like enter/tab/escape/backspace/delete/home/end/pageup/pagedown/up/down/left/right/f1..f24, or a single character)."}
                },
                "required": ["combo"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": "Scroll the mouse wheel on the user's desktop. dy is the number of notches: positive scrolls DOWN, negative scrolls UP. x,y (optional) are pixels of the last desktop_screenshot image like desktop_click; without them the screen centre is used. Requires user approval on every call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dy": {"type": "integer", "description": "Wheel notches; positive = down, negative = up (max 100)."},
                    "x": {"type": "integer", "description": "Optional horizontal position (pixels of the last screenshot image)."},
                    "y": {"type": "integer", "description": "Optional vertical position (same frame as x)."},
                    "coords": {"type": "string", "enum": ["screenshot", "screen"], "description": "Coordinate frame for x,y. Default screenshot."}
                },
                "required": ["dy"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Manage scheduled/automated tasks: list, create, edit, delete, pause, resume, or run tasks. Use this for ANY recurring/scheduled request ('every morning…', 'each day at 7:30', 'daily summarize…') — create a task rather than doing it once. Task types: llm (AI runs a prompt), research (runs the deep-research pipeline on a question), or action (built-in automation). Triggers can be time-based or event-based.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "edit", "delete", "pause", "resume", "run"],
                               "description": "The action to perform"},
                    "task_id": {"type": "string", "description": "Task ID (for edit/delete/pause/resume/run)"},
                    "name": {"type": "string", "description": "Task name"},
                    "prompt": {"type": "string", "description": "The instruction (for task_type=llm) or the research question (for task_type=research). Required for both."},
                    "task_type": {"type": "string", "enum": ["llm", "research", "action"],
                                  "description": "llm = AI runs your prompt; research = runs the deep-research pipeline on the prompt as a question; action = direct built-in function"},
                    "action_name": {"type": "string", "enum": [
                        "tidy_sessions", "tidy_documents", "consolidate_memory", "tidy_research",
                        "summarize_emails", "draft_email_replies", "extract_email_events",
                        "classify_events", "learn_sender_signatures",
                        "test_skills", "audit_skills", "check_email_urgency"
                    ],
                                    "description": "Built-in action (for task_type=action)"},
                    "trigger_type": {"type": "string", "enum": ["schedule", "event"],
                                     "description": "schedule = time-based, event = count-based"},
                    "schedule": {"type": "string", "enum": ["once", "daily", "weekly", "monthly", "cron"],
                                 "description": "Schedule frequency (for trigger_type=schedule)"},
                    "scheduled_time": {"type": "string", "description": "HH:MM in the explicit timezone. Ask for a missing time. Without timezone, legacy UTC semantics apply."},
                    "timezone": {"type": "string", "description": "IANA timezone, e.g. Europe/Madrid. Keep the local wall time stable through daylight-saving changes. Use the user's timezone, never guess from language."},
                    "scheduled_date": {"type": "string", "description": "For once: future ISO date and time, preferably with UTC offset. Without an offset, interpreted in timezone (UTC if omitted)."},
                    "cron_expression": {"type": "string", "description": "For cron: standard five-field cron expression in timezone (UTC if omitted)."},
                    "scheduled_day": {"type": "integer", "description": "Day of week 0=Mon (weekly) or day of month (monthly)"},
                    "trigger_event": {"type": "string", "enum": ["session_created", "message_sent", "document_created", "memory_added", "research_completed", "email_received", "skill_added"],
                                      "description": "Event name (for trigger_type=event)"},
                    "trigger_count": {"type": "integer", "description": "Fire every N events (for trigger_type=event)"},
                    "output_target": {"type": "string", "description": "Where results go. Defaults to 'session' (results land in a dedicated chat session the user reads) — this is the right choice for 'summarize for me' / 'send to me'. Do NOT go hunting for the user's email address; only use an email MCP tool name here if the user explicitly asked to be emailed AND an address is already known."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_calendar",
            "description": "Manage calendar events: list events in a date range, create, update, delete. Each event can carry a tag/category (event_type) and importance level. Resolve relative dates like today/tomorrow against the 'Current date and time' system context, then pass ISO 8601 datetimes in the user's local wall time; for all-day events set all_day=true and pass YYYY-MM-DD. For event reminders/alarms, pass reminder_minutes; the tool creates the Faustus note reminder, so do not also call manage_notes for the same reminder. Do not set rrule for single-occurrence requests such as 'next Wednesday only'; use rrule only when the user explicitly wants recurrence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list_events", "create_event", "update_event", "delete_event", "list_calendars"],
                               "description": "Action to perform"},
                    "summary": {"type": "string", "description": "Event title (for create/update)"},
                    "dtstart": {"type": "string", "description": "Start ISO datetime, or YYYY-MM-DD if all_day"},
                    "dtend": {"type": "string", "description": "End ISO datetime; defaults to +1h (or +1 day for all_day)"},
                    "all_day": {"type": "boolean", "description": "Whether this is an all-day event"},
                    "description": {"type": "string", "description": "Event description / notes"},
                    "location": {"type": "string", "description": "Event location"},
                    "uid": {"type": "string", "description": "Event UID (for update/delete)"},
                    "calendar_href": {"type": "string", "description": "Specific calendar URL (optional; defaults to first calendar)"},
                    "calendar": {"type": "string", "description": "Filter list_events by calendar name or href"},
                    "start": {"type": "string", "description": "list_events range start (ISO datetime). Use this for month/week requests after resolving the date range; do not pass a loose query string. Prefer start; backend also accepts start_time, start_date, range_start, from, dtstart, since."},
                    "end": {"type": "string", "description": "list_events range end (ISO datetime). Use this for month/week requests after resolving the date range; defaults to +14 days only when no range is requested. Prefer end; backend also accepts end_time, end_date, range_end, to, dtend, until."},
                    "event_type": {"type": "string", "description": "Tag / category for the event. Common values: work, personal, health, travel, meal, social, admin, other. Aliases accepted: tag, category, type."},
                    "importance": {"type": "string", "enum": ["low", "normal", "high", "critical"], "description": "Priority level (defaults to 'normal')"},
                    "reminder_minutes": {"type": "integer", "description": "For create_event: create an Faustus reminder this many minutes before the event, e.g. 5 for 'reminder 5 min before'."},
                    "rrule": {"type": "string", "description": "Recurrence rule in iCalendar RRULE format, e.g. 'FREQ=WEEKLY;BYDAY=MO' for weekly on Monday. Use with create_event or update_event. For update_event, pass an explicit empty string to remove recurrence and make the event single-occurrence."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_notes",
            "description": "Manage notes and checklists (Google Keep-style): list, view, add, update, delete, toggle_item. Use list/search to find candidate notes, then view with the note id when you need the full body. IMPORTANT: For to-do lists / checklists, set note_type='checklist' and pass the items as the `checklist_items` array — do NOT serialize them into `content` as plain text. For freeform notes, use note_type='note' and put the body in `content`. `due_date` accepts natural language like 'tomorrow at 9am' (parsed in the user's timezone) and fires a notification — do not also create a calendar event for the same reminder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "search", "view", "add", "update", "delete", "toggle_item"],
                               "description": "The action to perform"},
                    "id": {"type": "string", "description": "Note id (for update/delete/toggle_item); 8-char prefix is fine"},
                    "query": {"type": "string", "description": "Search text for action='search'"},
                    "title": {"type": "string", "description": "Note title (for add/update)"},
                    "content": {"type": "string", "description": "Freeform body text. Use this for note_type='note'. Do NOT use this for checklists — pass `checklist_items` instead."},
                    "note_type": {"type": "string", "enum": ["note", "checklist"],
                                  "description": "'note' = freeform text in `content`. 'checklist' = structured to-do items in `checklist_items`. Defaults to 'checklist' if checklist_items is supplied, else 'note'."},
                    "checklist_items": {"type": "array",
                                        "items": {"type": "object",
                                                  "properties": {
                                                      "text": {"type": "string", "description": "The to-do item text"},
                                                      "done": {"type": "boolean", "description": "Whether the item is checked off"}
                                                  },
                                                  "required": ["text"]},
                                        "description": "Checklist items for note_type='checklist'. Each item is {text, done}. REQUIRED for checklists — leaving this empty produces a blank note."},
                    "color": {"type": "string", "description": "Optional color label (e.g. 'yellow', 'blue', 'green')"},
                    "label": {"type": "string", "description": "Optional category label (also used as a list filter)"},
                    "pinned": {"type": "boolean", "description": "Pin the note to the top"},
                    "archived": {"type": "boolean", "description": "For update: archive/unarchive. For list: show archived notes when true."},
                    "due_date": {"type": "string", "description": "Reminder time. Accepts natural language ('tomorrow at 9am', '11pm today') or ISO 8601. Fires a notification at that time."},
                    "index": {"type": "integer", "description": "Checklist item index (for toggle_item, 0-based)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "api_call",
            "description": "Call a registered API integration (RSS reader, git forge, bookmark manager, smart home, etc.). Check the system context for available integrations and their endpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "integration": {"type": "string", "description": "Integration name or ID (e.g. 'Miniflux', 'Gitea')"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method"},
                    "path": {"type": "string", "description": "API endpoint path (e.g. '/v1/entries?status=unread&limit=20')"},
                    "body": {"type": "object", "description": "JSON request body (for POST/PUT/PATCH)"}
                },
                "required": ["integration", "method", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_teacher",
            "description": "Ask a more capable AI model for help when stuck on a difficult problem. The teacher provides guidance that can be saved as a learned skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Teacher model name (e.g. 'claude-sonnet-4') or 'auto' for configured default"},
                    "problem": {"type": "string", "description": "Describe the problem or question you need help with"}
                },
                "required": ["problem"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_skills",
            "description": (
                "Read or modify the user's skill library. Skills are SKILL.md files "
                "(YAML frontmatter + structured body: When to Use / Procedure / "
                "Pitfalls / Verification) and follow a draft → published lifecycle. "
                "Use progressive disclosure: 'list' to see what exists, 'view' to "
                "load full content for a single skill, 'view_ref' for sub-files. "
                "Use 'patch' for surgical text edits and 'edit' for full rewrites. "
                "'publish' once you've verified the procedure works. For add, "
                "always provide an explicit name slug and only tell the user the "
                "exact name returned by the tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "view", "view_ref", "add", "edit", "patch", "publish", "delete", "search"], "description": "list = name+description summary; view = full SKILL.md; view_ref = sub-file under the skill dir; add = create; edit = full rewrite (content); patch = old_string→new_string; publish = flip status; delete; search = relevance match on published skills."},
                    "name": {"type": "string", "description": "Slug/name of the skill. Required for add/view/view_ref/edit/patch/publish/delete. For add, choose the exact kebab-case name the user should see and report only the returned name."},
                    "path": {"type": "string", "description": "Sub-path under the skill directory for view_ref (e.g. 'references/example.md')."},
                    "description": {"type": "string", "description": "One-line summary surfaced in the skills index (for add)."},
                    "category": {"type": "string", "description": "Organizational grouping like 'dev', 'email', 'system' (for add)."},
                    "when_to_use": {"type": "string", "description": "Trigger conditions in plain English (for add)."},
                    "procedure": {"type": "array", "items": {"type": "string"}, "description": "Numbered steps (for add)."},
                    "pitfalls": {"type": "array", "items": {"type": "string"}, "description": "Known failure modes + recovery (for add)."},
                    "verification": {"type": "array", "items": {"type": "string"}, "description": "How to confirm the procedure succeeded (for add)."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Keyword tags (for add)."},
                    "platforms": {"type": "array", "items": {"type": "string"}, "description": "Restrict to OSes (for add)."},
                    "requires_toolsets": {"type": "array", "items": {"type": "string"}, "description": "Hide unless these toolsets are active (for add)."},
                    "fallback_for_toolsets": {"type": "array", "items": {"type": "string"}, "description": "Hide when these toolsets are active (for add)."},
                    "status": {"type": "string", "enum": ["draft", "published"], "description": "Defaults to 'draft' on add."},
                    "version": {"type": "string", "description": "Semver-ish, e.g. '1.0.0' (for add)."},
                    "confidence": {"type": "number", "description": "0-1 (for add/publish)."},
                    "content": {"type": "string", "description": "Full SKILL.md text (for edit)."},
                    "old_string": {"type": "string", "description": "Exact substring to replace (for patch). Must appear exactly once."},
                    "new_string": {"type": "string", "description": "Replacement text (for patch)."},
                    "query": {"type": "string", "description": "Search query (for search)."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_endpoints",
            "description": "Manage model API endpoints: list configured endpoints, add new ones, delete, enable or disable them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable"]},
                    "endpoint_id": {"type": "string", "description": "Endpoint ID (for delete/enable/disable)"},
                    "name": {"type": "string", "description": "Display name (for add)"},
                    "base_url": {"type": "string", "description": "API base URL e.g. https://api.openai.com/v1 (for add)"},
                    "api_key": {"type": "string", "description": "API key (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_mcp",
            "description": "Manage MCP (Model Context Protocol) tool servers: list servers and their tools, add new servers, delete, enable/disable, reconnect, or list all available tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable", "reconnect", "list_tools"]},
                    "server_id": {"type": "string", "description": "Server ID (for delete/enable/disable/reconnect)"},
                    "name": {"type": "string", "description": "Server name (for add)"},
                    "command": {"type": "string", "description": "Command to run e.g. npx (for add)"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments (for add)"},
                    "env": {"type": "object", "description": "Environment variables (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_webhooks",
            "description": "Manage webhooks: list, add, delete, enable or disable webhook endpoints.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "delete", "enable", "disable"]},
                    "webhook_id": {"type": "string", "description": "Webhook ID (for delete/enable/disable)"},
                    "name": {"type": "string", "description": "Webhook name (for add)"},
                    "url": {"type": "string", "description": "Webhook URL (for add)"},
                    "events": {"type": "string", "description": "Comma-separated event names (for add)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tokens",
            "description": "Manage API access tokens: list existing tokens, create new ones, or delete them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "delete"]},
                    "token_id": {"type": "string", "description": "Token ID (for delete)"},
                    "name": {"type": "string", "description": "Token name (for create)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_documents",
            "description": "Manage documents: list all documents (with optional search/language filter), delete documents, or run tidy cleanup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "delete", "tidy"]},
                    "document_id": {"type": "string", "description": "Document ID (for delete)"},
                    "search": {"type": "string", "description": "Search query (for list)"},
                    "language": {"type": "string", "description": "Filter by language (for list)"},
                    "limit": {"type": "integer", "description": "Max results (for list, default 50)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_settings",
            "description": "Manage user preferences and settings. Use `disable_tool`/`enable_tool`/`list_tools` to turn individual tools on or off globally (e.g. shell, search, browser, documents, memory, skills, images, tasks, notes, calendar, email). Use list/get/set/delete for free-form preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "set", "delete", "disable_tool", "enable_tool", "list_tools"]},
                    "key": {"type": "string", "description": "Setting key (for get/set/delete)"},
                    "value": {"description": "Setting value (for set) — can be string, number, boolean, or object"},
                    "tool": {"type": "string", "description": "Tool name to disable/enable (for disable_tool/enable_tool). Accepts aliases: shell, search, browser, documents, memory, skills, images, tasks, notes, calendar, email — or a raw tool name like 'bash' or 'web_search'."}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download_model",
            "description": "Download a HuggingFace model to a server. If `host` is omitted, defaults to the cookbook's currently-selected server (NOT localhost) — call list_cookbook_servers first if you're unsure where it should go.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "HuggingFace repo (e.g. 'Qwen/Qwen3-8B')"},
                    "host": {"type": "string", "description": "Target server — use the friendly NAME from list_cookbook_servers (e.g. 'gpu-box', 'workstation') or a raw user@host. Omit to use the cookbook's selected default server."},
                    "local": {"type": "boolean", "description": "Force download to THIS machine (localhost) instead of the default remote server."},
                    "include": {"type": "string", "description": "Glob filter for specific files (e.g. '*Q4_K_M*')"},
                },
                "required": ["repo_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "serve_model",
            "description": "Start serving a model with vLLM, SGLang, llama.cpp, Ollama, MLX Image, or Diffusers. If `host` is omitted, defaults to the cookbook's selected server (not localhost). For MLX image models on Apple Silicon use `python3 scripts/mlx_image_server.py --model <repo> --port 8100`; for non-MLX image/inpainting/diffusion models use `python3 scripts/diffusion_server.py --model <repo> --port 8100`. Never serve image models with `mlx_lm.server`; that is only for text/chat MLX models. After launching, call list_served_models to check readiness/errors; if it reports a diagnosis with retry suggestions, retry via serve_model using the suggested adjusted cmd.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "Model repo (e.g. 'Qwen/Qwen3-8B')"},
                    "cmd": {"type": "string", "description": "Full serve command (e.g. 'vllm serve <repo> --port 8000 --tp 2', 'python3 -m sglang.launch_server --model-path <repo> --port 30000', for MLX image models: 'python3 scripts/mlx_image_server.py --model <repo> --port 8100', or for non-MLX image models: 'python3 scripts/diffusion_server.py --model <repo> --port 8100')"},
                    "host": {"type": "string", "description": "Target server — friendly NAME from list_cookbook_servers (e.g. 'gpu-box', 'workstation') or raw user@host. Omit to use the cookbook's selected default."},
                    "local": {"type": "boolean", "description": "Force serve on THIS machine instead of the default remote server."},
                },
                "required": ["repo_id", "cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_served_models",
            "description": "List currently running model servers with status, model name, port, throughput, and structured Cookbook diagnoses. If a serve failed, this includes recent logs plus retry suggestions/adjusted commands the agent can use with serve_model.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stop_served_model",
            "description": "Stop a running model server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session ID of the server to stop"},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tail_serve_output",
            "description": "Read the last N lines of a cookbook serve/download task's tmux pane. Use ONLY in this exact sequence: (1) the user asked to serve a model, (2) you launched it via serve_model, (3) list_served_models reports the NEW task as crashed/error, (4) call tail_serve_output on the new sessionId to find the root cause, (5) call serve_model again with adjusted flags. DO NOT call this on old stopped/completed download tasks — they are historical and won't tell you anything about the current attempt. DO NOT investigate past failures before launching; the environment may have changed since.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session id from list_served_models (e.g. 'serve-abc12345', 'cookbook-a1b2c3d4')."},
                    "tail": {"type": "integer", "description": "How many lines of pane scrollback to fetch (default 300, max 4000). Bump this if the error in the visible tail references an earlier line ('see root cause above')."},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_downloads",
            "description": "List in-progress model downloads in the Cookbook. Shows each download's model name, phase, percent (if available), session ID, and remote host.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_download",
            "description": "Cancel an in-progress model download by killing its tmux session. Use list_downloads first to get the session_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Tmux session ID from list_downloads (e.g. 'cookbook-a1b2c3d4')"},
                },
                "required": ["session_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_hf_models",
            "description": "Search HuggingFace for models matching a query. Returns a ranked list of repo IDs, sizes (when available), and download counts. Use this when the user wants to find a model to download.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms (e.g. 'Qwen 8B', 'flux', 'llama-3 instruct')"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_cookbook_servers",
            "description": "List the cookbook's configured servers (remote GPU boxes + local) and the current default host. Call this before download_model/serve_model when the user didn't specify a host, so models go to the right machine (where the GPUs and model cache are) instead of localhost. If multiple servers and intent is ambiguous, show them and ask the user which.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_serve_presets",
            "description": "List saved Cookbook serve presets. Each preset is a launch template (name, model, host, port, tmux cmd) the user previously saved from the UI. Call this BEFORE raw serve_model when the user asks to launch a model by name manually.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "adopt_served_model",
            "description": "Register an existing tmux model server (started manually or outside the cookbook flow) into Cookbook tracking, AND add it as a chat endpoint. Use when the user (or you) launched something via ssh+tmux and now want it visible in the UI / stoppable via stop_served_model / usable in the model picker. Verifies the tmux session + port respond before adding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Remote host in user@host form (e.g. 'user@192.0.2.10'). Omit for localhost."},
                    "tmux_session": {"type": "string", "description": "Existing tmux session name (e.g. 'minimax-m27')"},
                    "model": {"type": "string", "description": "Model repo_id or display name (e.g. 'cyankiwi/MiniMax-M2.7-AWQ-4bit')"},
                    "port": {"type": "integer", "description": "Port the server is listening on (default 8000)"},
                    "name": {"type": "string", "description": "Optional display name (defaults to model basename)"},
                    "add_endpoint": {"type": "boolean", "description": "Also register as a chat endpoint (default true)"}
                },
                "required": ["tmux_session", "model"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "serve_preset",
            "description": "Launch a saved Cookbook serve preset by name. Reuses the exact tmux command + host the user saved before. This is the preferred way to start a known model (SD3.5, vLLM presets, etc.) — don't fabricate launch commands when a preset exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Preset name (exact or case-insensitive substring of one returned by list_serve_presets)"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_cached_models",
            "description": "List models already cached on disk locally or on a remote server. `host` accepts friendly Cookbook server names from list_cookbook_servers (for example workstation) or raw user@host. Also reports completed Cookbook download tasks when the filesystem cache scan cannot locate the HF cache path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Friendly Cookbook server name (e.g. 'workstation', 'gpu-box') or raw remote host (e.g. 'user@gpu-box'). Omit for local."},
                    "model_dir": {"type": "string", "description": "Comma-separated additional model directories to scan beyond ~/.cache/huggingface/hub"},
                    "ssh_port": {"type": "string", "description": "SSH port for remote host (default 22)"},
                    "platform": {"type": "string", "enum": ["linux", "windows"], "description": "Remote platform"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "app_api",
            "description": "Generic loopback to allowed internal Faustus endpoints. Use this when there's no named tool for what the user wants. Hits the same routes the UI buttons hit (cookbook, gallery, library/documents, memory, notes, calendar, tasks, settings, themes, research, compare, etc.). action='endpoints' returns the OpenAPI surface (use `filter` to narrow). action='call' (default) takes method+path+body. Sensitive auth/user/admin/shell paths and host-control Cookbook mutation routes are blocked for safety. Do not use for shell commands; use named command tooling instead. Do not use for package installs, engine rebuilds, PID signalling, or email account discovery; use list_email_accounts for email accounts because /api/email/accounts is owner-filtered in tool context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["call", "endpoints"], "description": "'call' to hit an endpoint, 'endpoints' to list what's available"},
                    "path": {"type": "string", "description": "Endpoint path starting with /api/ (e.g. '/api/cookbook/gpus', '/api/gallery/list', '/api/calendar/events')"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method (default GET)"},
                    "body": {"type": "object", "description": "JSON request body for POST/PUT/PATCH"},
                    "query": {"type": "object", "description": "Querystring params as a key-value object"},
                    "filter": {"type": "string", "description": "For action=endpoints: substring to filter paths/summaries (e.g. 'cookbook', 'gallery')"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": "Edit a gallery image: upscale, remove background, inpaint, or harmonize.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string", "description": "Gallery image ID"},
                    "action": {"type": "string", "enum": ["upscale", "rembg", "inpaint", "harmonize"], "description": "Edit action"},
                    "prompt": {"type": "string", "description": "For inpaint: what to fill the masked area with"},
                    "scale": {"type": "number", "description": "For upscale: scale factor (default 2)"},
                    "mask_id": {"type": "string", "description": "Required for inpaint: owned gallery mask ID, same dimensions as source; white redraws, black preserves"},
                    "strength": {"type": "number", "minimum": 0, "maximum": 1, "description": "Inpaint/harmonize edit strength"},
                },
                "required": ["image_id", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_research",
            "description": "Start a deep research task on a topic. Returns a task ID for tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Research question or topic"},
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_contact",
            "description": "Look up a contact by name. Searches CardDAV address book and sent email history. Returns email addresses (when available) or phone numbers. Use when the user says 'message [name]', 'email [name]', or asks for someone's contact details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Person's name to search for"},
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_contact",
            "description": "Create, update, delete, or list the user's CardDAV contacts. Use to save a new contact, update an existing one (email/phone/address), or remove one. Add does not require email: name + phone or name + address is valid. For update/delete you need the contact's uid — call action='list' first to find it. Writes go through the same dedupe + validation as the Contacts UI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "update", "delete"],
                               "description": "list = show all contacts (with uids); add = create; update = edit by uid; delete = remove by uid."},
                    "uid": {"type": "string", "description": "Contact UID (required for update/delete; get it from action=list)."},
                    "name": {"type": "string", "description": "Contact's display name (for add/update)."},
                    "email": {"type": "string", "description": "Single email address (convenience for add, or the primary email for update). Optional when phone or address is provided."},
                    "emails": {"type": "array", "items": {"type": "string"}, "description": "Full list of email addresses (first is primary)."},
                    "phones": {"type": "array", "items": {"type": "string"}, "description": "Full list of phone numbers. Valid for add/update."},
                    "address": {"type": "string", "description": "Postal/mailing address as a single human-readable string."},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_email_accounts",
            "description": "List configured email accounts. Use this before checking mail when the user names a mailbox/account such as Gmail, work, or a custom domain, then pass the returned account name/email/id to the other email tools.",
            "parameters": {
                "type": "object",
                "properties": {},
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send a new email. Use resolve_contact first if you only have a name and need to find the email address. If multiple accounts exist, pass account from list_email_accounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Email body text"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
                "required": ["to", "subject", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_emails",
            "description": "List emails from an account/folder, newest first. Returns subject, sender, date, UID, and account for each email. Use list_email_accounts first when the user mentions Gmail/work/a custom mailbox. For last/latest/newest email requests, use max_results=1 and unread_only=false.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "max_results": {"type": "integer", "description": "Max emails to return (default: 20)"},
                    "limit": {"type": "integer", "description": "Backward-compatible alias for max_results"},
                    "unread_only": {"type": "boolean", "description": "Only show unread emails. Default false; set true only when the user asks for unread emails."},
                    "unresponded_only": {"type": "boolean", "description": "Only show unanswered emails. Default false."},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the full content of a specific email by UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID to read"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, especially when the UID came from a non-default mailbox"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scan_email_unsubscribes",
            "description": "Scan recent email headers for likely spam/newsletter unsubscribe candidates. Does not unsubscribe anything. Review candidates with the user before acting; mailto methods can be executed with unsubscribe_email, web URL methods require browser/web tools after approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "IMAP folder to scan (default: INBOX)"},
                    "limit": {"type": "integer", "description": "Maximum candidates to return (default: 25)"},
                    "max_scan": {"type": "integer", "description": "How many newest emails to inspect (default: 150)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts"},
                },
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "unsubscribe_email",
            "description": "Execute one approved unsubscribe action for an email UID. Safe mailto List-Unsubscribe methods are sent/staged. Web URL methods return a requires-browser instruction and exact URL; use browser/web tools only after user approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from scan_email_unsubscribes/list_emails"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "method_index": {"type": "integer", "description": "Method index from scan_email_unsubscribes (default: 0)"},
                    "allow_web": {"type": "boolean", "description": "Return browser/web instructions when selected method is URL"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reply_to_email",
            "description": "SEND a reply email immediately by UID. Do not use this when the user asks to write/draft/open/start a reply; use ui_control action=open_email_reply with body instead so the user can review. Only use when the user explicitly says to send now. Use the exact UID from the latest read_email/list_emails result; never invent UID 1. Automatically threads with In-Reply-To/References headers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Exact UID of the email to reply to from list_emails/read_email; never invent UID 1"},
                    "body": {"type": "string", "description": "Reply body text"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Optional account name/email/id from list_email_accounts, especially when the UID came from a non-default mailbox"},
                },
                "required": ["uid", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_email",
            "description": "Perform one action on many emails at once. Use this for 'delete all those', 'archive these', 'mark all read', or any bulk operation after list_emails. Always pass account when the listed emails came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["mark_read", "mark_unread", "archive", "delete", "junk"], "description": "Bulk action to perform"},
                    "uids": {"type": "array", "items": {"type": "string"}, "description": "UIDs from the latest list_emails result"},
                    "all_unread": {"type": "boolean", "description": "Operate on all unread messages in folder instead of explicit UIDs"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "permanent": {"type": "boolean", "description": "For delete: hard-delete instead of moving to Trash"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts, e.g. Gmail or user@example.com"},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_email",
            "description": "Delete one email by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "permanent": {"type": "boolean", "description": "Hard-delete instead of moving to Trash"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "archive_email",
            "description": "Archive one email by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_email_read",
            "description": "Mark one email as read or unread by UID. For multiple messages, use bulk_email instead. Always pass account when the email came from a named account such as Gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Email UID from list_emails/read_email"},
                    "folder": {"type": "string", "description": "IMAP folder (default: INBOX)"},
                    "read": {"type": "boolean", "description": "True marks read; false marks unread"},
                    "account": {"type": "string", "description": "Account name/email/id from list_email_accounts"},
                },
                "required": ["uid"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_bg_jobs",
            "description": "Inspect and control detached background `bash` jobs (started with the `#!bg` marker). action='list' shows this chat's jobs with id/status/age/command; action='output' returns a job's captured output so far (use for a still-running job, or to re-read a finished one); action='kill' terminates a runaway job's process tree instead of waiting out its max-runtime. output and kill need job_id from list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "output", "kill"], "description": "list | output | kill (default: list)"},
                    "job_id": {"type": "string", "description": "Background job id (required for output/kill; from action='list')"},
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rename_symbol",
            "description": "Structure-assisted rename (EDIT-06): uses the code index to touch only lines that actually reference the symbol, never an unrelated string/comment/doc that happens to share the name. action='plan' (default) returns every definition/caller/test site with nothing written (the impact view); action='apply' performs the rename and returns the ChangeSet. Refuses (rather than falling back to a workspace-wide text substitution) when the symbol is not indexed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_name": {"type": "string", "description": "The current identifier name"},
                    "new_name": {"type": "string", "description": "The new identifier name (must be a valid identifier)"},
                    "action": {"type": "string", "enum": ["plan", "apply"], "description": "plan (default, no writes) | apply"},
                    "path": {"type": "string", "description": "Workspace/project root (optional; defaults to the project root)"},
                    "project_id": {"type": "string", "description": "Optional project scope"}
                },
                "required": ["old_name", "new_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "install_dependencies",
            "description": "Install project dependencies WITH CONTROL (EXEC-05): detects the manager from the project's lockfile/manifest, validates every package name (rejects a URL/path/shell fragment), and never installs globally (project-local target / active virtualenv only). action='plan' (default) returns the plan and a plan_hash without running anything; action='install' only runs when the caller ALSO passes approved=true (after the user reviewed the plan) — a changed plan hashes differently and must be approved again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "packages": {"type": "array", "items": {"type": "string"}, "description": "Package names/specs to install"},
                    "action": {"type": "string", "enum": ["plan", "install"], "description": "plan (default) | install"},
                    "project_root": {"type": "string", "description": "Project root containing the lockfile/manifest (optional; defaults to the project root)"},
                    "approved": {"type": "boolean", "description": "Required (true) to actually run action='install' once the plan has been reviewed"}
                },
                "required": ["packages"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_scripts",
            "description": "Save and run reusable, VERSIONED command recipes (EXEC-06). action='save' saves/version-bumps a script under `name` (never overwrites a prior version). action='run' renders declared `params` into the saved argv (a plain list, never a shell string — no injection surface) and runs it locally. action='run_remote' does the same over SSH to an alias PAIRED earlier out-of-band; `host`/`fingerprint` must match the paired target EXACTLY, so a similar hostname never inherits another target's trust.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["save", "run", "run_remote"], "description": "save | run | run_remote"},
                    "name": {"type": "string", "description": "The script's saved name"},
                    "command_template": {"type": "string", "description": "save: the argv template, e.g. 'grep {pattern} {path}'"},
                    "params": {"type": "array", "items": {"type": "string"}, "description": "save: the declared parameter names the template may reference"},
                    "values": {"type": "object", "description": "run/run_remote: {param: value} for the saved script's declared params"},
                    "cwd": {"type": "string", "description": "run: working directory (optional; defaults to the project root)"},
                    "alias": {"type": "string", "description": "run_remote: the paired SSH target alias"},
                    "host": {"type": "string", "description": "run_remote: the host presented for alias (must match the paired one exactly)"},
                    "fingerprint": {"type": "string", "description": "run_remote: the host key fingerprint presented for alias (must match the paired one exactly)"}
                },
                "required": ["action", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_desktop_control",
            "description": "DESK-01 safety policy for THIS chat session's desktop control: an allowlist of windows/apps desktop_* tools may act on, and an opt-in audit trail (before/after screen hash per action). Both are opt-in — a session that never calls this keeps the default unrestricted behaviour. action='set_allowlist' with `titles` (substrings) pauses any desktop_click/type/key/scroll the moment focus moves to a window not on the list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get_allowlist", "set_allowlist", "clear_allowlist", "enable_audit", "disable_audit", "audit_log"], "description": "Which policy operation to perform"},
                    "titles": {"type": "array", "items": {"type": "string"}, "description": "set_allowlist: window-title substrings authorized for desktop control"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "capture_evidence",
            "description": "Turn a screenshot (from desktop_screenshot or any browser capture already taken this turn) into WEB-05 evidence: resolution/scale/viewport/timestamp recorded, and a pixel region/point as the edit reference (never a fabricated line of code). action='build' (default) records a capture and its EvidenceRef; action='compare' diffs two prior `capture` results (same_page/image_changed/dom_changed); action='check_stale' answers whether a prior capture may still be used as an edit point for the CURRENT page (WEB-05's acceptance criterion) — never assume yes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["build", "compare", "check_stale"], "description": "build (default) | compare | check_stale"},
                    "url": {"type": "string", "description": "build: the page URL the screenshot was taken on"},
                    "title": {"type": "string", "description": "build: the page title"},
                    "width": {"type": "integer", "description": "build: captured image width in pixels"},
                    "height": {"type": "integer", "description": "build: captured image height in pixels"},
                    "scale": {"type": "number", "description": "build: device/scale factor applied to the capture (default 1.0)"},
                    "viewport_width": {"type": "integer", "description": "build: browser viewport width, if different from the image"},
                    "viewport_height": {"type": "integer", "description": "build: browser viewport height, if different from the image"},
                    "image_b64": {"type": "string", "description": "build: base64 image data (from the prior screenshot tool's result)"},
                    "dom_hash": {"type": "string", "description": "build: a fingerprint of the page's DOM/accessibility snapshot at capture time, for staleness checks"},
                    "region": {"type": "object", "description": "build: optional crop {x,y,width,height,selector?} in pixels of the captured image"},
                    "annotation": {"type": "object", "description": "build: optional annotated point {x,y,label?} in pixels of the captured image"},
                    "before": {"type": "object", "description": "compare: a prior `capture` result"},
                    "after": {"type": "object", "description": "compare: a later `capture` result"},
                    "capture": {"type": "object", "description": "check_stale: a prior `capture` result"},
                    "current_url": {"type": "string", "description": "check_stale: the URL of the page as it is right now"},
                    "current_dom_hash": {"type": "string", "description": "check_stale: a fresh DOM fingerprint of the page as it is right now"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browser_extract",
            "description": "Complex navigation/extraction with declared limits (WEB-06): pagination stops at max_pages/max_items and says WHY (truncated/reason), a restricted-access page is reported as restricted rather than scraped around, a download target is always a single sandboxed file (never a folder), and a download is verified complete (hash/size) before it may be treated as finished — a transfer cut short must never be reported complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["paginate", "detect_restricted_access", "check_form_fill", "select_download_target", "verify_download", "check_login_reuse"], "description": "Which extraction operation to perform"},
                    "pages": {"type": "array", "items": {"type": "object"}, "description": "paginate: already-fetched pages, each {items: [...], has_next: bool}"},
                    "max_pages": {"type": "integer", "description": "paginate: page limit (default 20)"},
                    "max_items": {"type": "integer", "description": "paginate: item limit (default 2000)"},
                    "page_text": {"type": "string", "description": "detect_restricted_access: the page's visible text"},
                    "fields": {"type": "object", "description": "check_form_fill: {field_name: value} to validate against limits"},
                    "max_fields": {"type": "integer", "description": "check_form_fill: field-count limit (default 50)"},
                    "max_value_chars": {"type": "integer", "description": "check_form_fill: per-value character limit (default 4000)"},
                    "sandbox_root": {"type": "string", "description": "select_download_target: the confined root directory (optional; defaults to the project root)"},
                    "filename": {"type": "string", "description": "select_download_target: the file name offered by the site (any directory component is discarded)"},
                    "path": {"type": "string", "description": "verify_download: the path the file was actually written to"},
                    "expected_sha256": {"type": "string", "description": "verify_download: the expected sha256, if known in advance"},
                    "expected_bytes": {"type": "integer", "description": "verify_download: the expected exact size in bytes, if known in advance"},
                    "expected_total_bytes": {"type": "integer", "description": "verify_download: the total size the transfer reported it would send"},
                    "task_id": {"type": "string", "description": "check_login_reuse: the browser session's task id (from the browser tool that opened it)"}
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "manage_spreadsheet",
            "description": "Reliable CSV/XLSX handling (ART-04): typed CSV import/export (IDs, dates and formulas are classified, never silently mangled), formula-injection is blocked on export unless explicitly allowed, and an XLSX preview flags which formula cells have NO cached value (need recalculation) before their value is trusted. action='import_csv'|'export_csv'|'read_workbook'|'write_range'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["import_csv", "export_csv", "read_workbook", "write_range"], "description": "Which spreadsheet operation to perform"},
                    "text": {"type": "string", "description": "import_csv: the raw CSV/TSV text"},
                    "dialect": {"type": "string", "description": "import_csv: CSV dialect hint if sniffing should be skipped (default 'excel')"},
                    "rows": {"type": "array", "items": {"type": "array"}, "description": "export_csv: rows to export, each a list of cell values"},
                    "allow_formulas": {"type": "boolean", "description": "export_csv: keep leading =/+/-/@ live instead of neutralizing it (only for a sheet that legitimately has formulas)"},
                    "path": {"type": "string", "description": "read_workbook/write_range: path to the .xlsx file"},
                    "max_rows": {"type": "integer", "description": "read_workbook: rows to preview per sheet (default 200)"},
                    "sheet_name": {"type": "string", "description": "read_workbook: one sheet only (optional, defaults to all); write_range: the sheet to write into"},
                    "start_cell": {"type": "string", "description": "write_range: top-left cell of the block, e.g. 'B2'"},
                    "values": {"type": "array", "items": {"type": "array"}, "description": "write_range: a 2D block of values to write, starting at start_cell"},
                    "save_as": {"type": "string", "description": "write_range: save to a different path instead of writing in place (optional)"}
                },
                "required": ["action"]
            }
        }
    },
    # ── Git tools (Lote 87, OBJ-4): "commit this and push", done through
    # tools that respect the user's repository policy and show up in the
    # Source control panel — see docs/api/git.md "Herramientas del agente".
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Git status of a repo: current branch, ahead/behind its upstream, staged/unstaged/untracked/conflicted files, and the last `limit` commits. Read-only. Every git tool accepts an optional `path` or `repo` (see below) -- when neither is given, and the project has more than one repo and the active workspace isn't inside any of them, the call is refused with error_class git.which_repo naming the repo choices; ask the user which one. `path` is confined to this turn's workspace / the session's project folders; a repo outside that is refused.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo to inspect (optional; defaults to the active workspace)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "How many recent commits to include (default 10)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Commit history for the repo at `path` (or the active workspace). Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Commits to return (default 20)"},
                    "ref": {"type": "string", "description": "Branch/ref to walk (default HEAD); 'all' walks every ref"},
                    "cursor": {"type": "string", "description": "Page cursor from a prior call's `next_cursor` (optional)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Diff for the repo at `path`: the working tree (default), the index (`staged: true`), or one `commit`. Add `path_in_repo` to limit to one file. Text is clipped to 60 KB. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "path_in_repo": {"type": "string", "description": "Limit the diff to this one file, relative to the repo root (optional)"},
                    "staged": {"type": "boolean", "description": "Diff the index against HEAD instead of the working tree against the index (ignored when `commit` is set)"},
                    "commit": {"type": "string", "description": "Diff this commit against its parent instead of the working tree (a sha or ref)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch",
            "description": "Create a branch in the repo at `path` (checked out by default). Refused with a clear reason when the repo's git agent policy has use_branch=false, unless the user explicitly approved this exact call (see docs/api/git.md).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "name": {"type": "string", "description": "New branch name"},
                    "start_point": {"type": "string", "description": "Commit/branch to start from (optional; defaults to HEAD)"},
                    "checkout": {"type": "boolean", "description": "Check out the new branch immediately (default true)"},
                    "user_confirmed": {"type": "boolean", "description": "Set true ONLY after the user explicitly approved this exact action in this conversation (e.g. via ask_user) — required when the repo's policy would otherwise refuse it"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_checkout",
            "description": "Switch the repo at `path` to an existing `branch`. Refused with a clear reason when the repo's git agent policy has use_branch=false, unless the user explicitly approved this exact call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "branch": {"type": "string", "description": "Branch to check out"},
                    "user_confirmed": {"type": "boolean", "description": "Set true ONLY after the user explicitly approved this exact action — required when the repo's policy would otherwise refuse it"}
                },
                "required": ["branch"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_merge",
            "description": "Merge `branch` into the current branch of the repo at `path`. `ff` controls fast-forwarding: 'auto' (default — fast-forward when possible, else a merge commit), 'only' (refuse unless fast-forwardable), 'no' (always create a merge commit, even when a fast-forward would do). On conflict the merge is aborted automatically and reported (error_class git.merge_conflict, with the conflicting paths) — conflicts are never left for the model to resolve; point the user at the Source control panel instead. Refused with a clear reason when the repo's git agent policy has use_branch=false, unless the user explicitly approved this exact call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "branch": {"type": "string", "description": "Branch (or ref) to merge into the current branch"},
                    "ff": {"type": "string", "enum": ["auto", "only", "no"], "description": "Fast-forward mode (default 'auto')"},
                    "message": {"type": "string", "description": "Merge commit message (optional; used only when a merge commit is created)"},
                    "user_confirmed": {"type": "boolean", "description": "Set true ONLY after the user explicitly approved this exact action — required when the repo's policy would otherwise refuse it"}
                },
                "required": ["branch"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_delete_branch",
            "description": "Delete local branch `name` in the repo at `path`. Refuses to delete the currently checked out branch. Without `force`, refuses (error_class git.branch_unmerged) when the branch is not fully merged. Refused with a clear reason when the repo's git agent policy has use_branch=false, unless the user explicitly approved this exact call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "name": {"type": "string", "description": "Branch to delete"},
                    "force": {"type": "boolean", "description": "Delete even if not fully merged (default false)"},
                    "user_confirmed": {"type": "boolean", "description": "Set true ONLY after the user explicitly approved this exact action — required when the repo's policy would otherwise refuse it"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage EXACTLY the files listed in `paths` (never everything) and commit them in the repo at `path`, using the repo's own configured author identity. Refused with a clear reason when the repo's git agent policy has commit=false, unless the user explicitly approved this exact call, or when the repo has no user.name/user.email configured.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "message": {"type": "string", "description": "Commit message"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Files to stage and commit, relative to the repo root or the active workspace. Required — never staged implicitly."},
                    "amend": {"type": "boolean", "description": "Amend the previous commit instead of creating a new one (default false)"},
                    "user_confirmed": {"type": "boolean", "description": "Set true ONLY after the user explicitly approved this exact action — required when the repo's policy would otherwise refuse it"}
                },
                "required": ["message", "paths"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": "Push the repo at `path` to a remote. Never force (there is no force option). Refused with a clear reason when the repo's git agent policy has push=false, unless the user explicitly approved this exact call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "remote": {"type": "string", "description": "Remote name (optional; defaults to origin)"},
                    "branch": {"type": "string", "description": "Branch to push (optional; defaults to the current branch)"},
                    "set_upstream": {"type": "boolean", "description": "Set the pushed branch's upstream (-u) (default false)"},
                    "user_confirmed": {"type": "boolean", "description": "Set true ONLY after the user explicitly approved this exact action — required when the repo's policy would otherwise refuse it"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_pull",
            "description": "Pull the repo at `path` from its remote, fast-forward only (never a merge). Fails with git.diverged if the local branch and its upstream have diverged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "remote": {"type": "string", "description": "Remote name (optional; defaults to origin)"},
                    "branch": {"type": "string", "description": "Branch to pull (optional; defaults to the current branch's upstream)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "git_fetch",
            "description": "Fetch remote-tracking refs for the repo at `path`, without touching the working tree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path inside the repo (optional). Resolution order when omitted: `repo` (by name) > the repo containing the active workspace > the project's only repo (if it has just one) > ambiguous (error git.which_repo, listing the repo names) when the project has several and none of those picked one."},
                    "repo": {"type": "string", "description": "Repo name instead of `path` -- matched by exact name, or a unique case-insensitive prefix, among the repos linked to this session's project. Usually unnecessary: leave both `path` and `repo` empty to use the active workspace's repo, or the project's only repo."},
                    "remote": {"type": "string", "description": "Remote name (optional; defaults to origin)"},
                    "prune": {"type": "boolean", "description": "Remove remote-tracking refs deleted on the remote (default false)"}
                },
                "required": []
            }
        }
    },
]


# ---------------------------------------------------------------------------
# Converter: native function call -> ToolBlock
# ---------------------------------------------------------------------------

def _decode_loose_json_string(value: str) -> str:
    """Decode common JSON string escapes without requiring inner quotes to be escaped."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\" or i + 1 >= len(value):
            out.append(ch)
            i += 1
            continue
        nxt = value[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "b":
            out.append("\b")
        elif nxt == "f":
            out.append("\f")
        elif nxt in ('"', "\\", "/"):
            out.append(nxt)
        elif nxt == "u" and i + 5 < len(value):
            try:
                out.append(chr(int(value[i + 2:i + 6], 16)))
                i += 4
            except ValueError:
                out.append("\\" + nxt)
        else:
            out.append("\\" + nxt)
        i += 2
    return "".join(out)


def _repair_document_function_args(tool_type: str, arguments: str) -> Optional[dict]:
    """Salvage obvious malformed document tool args from local model wrappers.

    The doc LoRA sometimes emits the right native tool call but puts raw quotes
    inside the document text, making the surrounding JSON invalid. Treat that as
    a wrapper parse failure, not a semantic tool-choice failure.
    """
    if tool_type != "update_document" or not isinstance(arguments, str):
        return None
    raw = arguments.strip()
    if not raw.startswith("{") or not raw.endswith("}"):
        return None
    for key in ("content", "conten"):
        marker = f'"{key}"'
        key_pos = raw.find(marker)
        if key_pos < 0:
            continue
        colon_pos = raw.find(":", key_pos + len(marker))
        if colon_pos < 0:
            continue
        first_quote = raw.find('"', colon_pos + 1)
        if first_quote < 0:
            continue
        close_brace = raw.rfind("}")
        last_quote = raw.rfind('"', first_quote + 1, close_brace)
        if last_quote <= first_quote:
            continue
        content = _decode_loose_json_string(raw[first_quote + 1:last_quote])
        return {"content": content}
    return None


def function_call_to_tool_block(name: str, arguments: str) -> Optional[ToolBlock]:
    """Convert a native function call into a ToolBlock for the existing execution pipeline."""
    tool_type = _TOOL_NAME_MAP.get(name, name)
    try:
        if not arguments or (isinstance(arguments, str) and not arguments.strip()):
            args = {}
        else:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (json.JSONDecodeError, TypeError):
        args = _repair_document_function_args(tool_type, arguments)
        if args is not None:
            logger.warning(f"Repaired malformed document function call arguments for {name}")
        else:
            logger.error(f"Failed to parse function call arguments for {name}: {arguments}")
            return None

    # Some models emit valid JSON that isn't an object (e.g. a bare array
    # ["ls -la"], string, or number) as function arguments. Most local tools keep
    # the legacy empty-object coercion for stream robustness, but email MCP tools
    # must fail closed so a malformed call cannot read the default mailbox.
    # Uses the shared BUILTIN_EMAIL_TOOLS (single source of truth) so the
    # fail-closed set can't drift from the dispatch/blocklist sets.
    if not isinstance(args, dict):
        if tool_type.startswith("mcp__email__") or name in BUILTIN_EMAIL_TOOLS:
            logger.warning(f"Non-object email function call arguments for {name}: {args!r}; rejecting")
            return None
        logger.warning(f"Non-object function call arguments for {name}: {args!r}; treating as empty")
        args = {}

    required_args = _REQUIRED_NATIVE_TOOL_ARGS.get(tool_type)
    if required_args and not any(str(args.get(key) or "").strip() for key in required_args):
        logger.warning(f"Rejecting empty required arguments for function call {name}: {args!r}")
        return None

    # Allow MCP tools through (namespaced as mcp__serverid__toolname)
    if tool_type.startswith("mcp__"):
        content = json.dumps(args) if args else "{}"
        return ToolBlock(tool_type, content)
    # Email tools are implemented as MCP — route them to email
    if name in BUILTIN_EMAIL_TOOLS:
        return ToolBlock(f"mcp__email__{name}", json.dumps(args) if args else "{}")
    if tool_type not in TOOL_TAGS:
        logger.warning(f"Unknown function call: {name}")
        return None

    # Convert structured args back to the text format each tool expects
    if tool_type == "bash":
        content = args.get("command", "")
    elif tool_type == "python":
        content = args.get("code", "")
    elif tool_type == "web_search":
        queries = args.get("queries")
        if isinstance(queries, list) and queries:
            content = str(queries[0])
        elif queries:
            content = str(queries)
        else:
            content = args.get("query", "")
        # Preserve the model-requested freshness filter — the web_search schema
        # advertises time_filter and the executor parses {"query","time_filter"},
        # but a bare query string dropped it. Mirrors the read_file JSON idiom.
        tf = args.get("time_filter")
        if content and isinstance(tf, str) and tf in ("day", "week", "month", "year"):
            content = json.dumps({"query": content, "time_filter": tf})
    elif tool_type == "read_file":
        # Plain path (back-compat) unless a line range is requested → JSON.
        if args.get("offset") or args.get("limit"):
            content = json.dumps(args)
        else:
            content = args.get("path", "")
    elif tool_type in ("grep", "glob", "ls", "inspect_media", "plan_media_transform", "transform_media"):
        content = json.dumps(args) if args else "{}"
    elif tool_type in ("find_symbol", "callers", "tests_for"):
        content = json.dumps(args) if args else "{}"
    elif tool_type == "get_workspace":
        content = ""
    elif tool_type == "write_file":
        content = args.get("path", "") + "\n" + args.get("content", "")
    elif tool_type == "edit_file":
        content = json.dumps(args)
    elif tool_type == "apply_patch":
        content = args.get("patch_text") or args.get("patchText") or args.get("patch") or ""
    elif tool_type in ("todowrite", "delegate_agents"):
        content = json.dumps(args)
    elif tool_type == "create_document":
        parts = [args.get("title", "Untitled")]
        if args.get("language"):
            parts.append(args["language"])
        parts.append(args.get("content", ""))
        content = "\n".join(parts)
    elif tool_type == "edit_document":
        blocks = []
        edits = args.get("edits", [])
        if not isinstance(edits, list):
            edits = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            blocks.append(
                f'<<<FIND>>>\n{edit.get("find", "")}\n<<<REPLACE>>>\n{edit.get("replace", "")}\n<<<END>>>'
            )
        content = "\n".join(blocks)
    elif tool_type == "suggest_document":
        blocks = []
        suggestions = args.get("suggestions", [])
        if not isinstance(suggestions, list):
            suggestions = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            blocks.append(
                f'<<<FIND>>>\n{s.get("find", "")}\n<<<SUGGEST>>>\n{s.get("replace", "")}\n<<<REASON>>>\n{s.get("reason", "")}\n<<<END>>>'
            )
        content = "\n".join(blocks)
    elif tool_type == "update_document":
        content = args.get("content", "")
    elif tool_type in ("search_chats", "search_project_chats"):
        content = args.get("query", "")
    elif tool_type == "project_context":
        content = json.dumps(args)
    elif tool_type == "manage_project_context":
        content = json.dumps(args)
    elif tool_type == "project_objectives":
        content = json.dumps(args)
    elif tool_type in ("manage_teach_mode", "capability_health", "branch_futures"):
        content = json.dumps(args)
    elif tool_type == "memory_rules":
        content = json.dumps(args)
    elif tool_type == "expert_review":
        content = json.dumps(args)
    elif tool_type == "verify_claim":
        content = json.dumps(args)
    elif tool_type == "chat_with_model":
        content = args.get("model", "") + "\n" + args.get("message", "")
    elif tool_type == "create_session":
        content = args.get("name", "Untitled") + "\n" + args.get("model", "")
    elif tool_type == "list_sessions":
        content = args.get("filter", "")
    elif tool_type == "send_to_session":
        content = args.get("session_id", "") + "\n" + args.get("message", "")
    elif tool_type == "pipeline":
        # Pass as JSON for the pipeline parser
        content = json.dumps({"steps": args.get("steps", [])})
    elif tool_type == "manage_session":
        action = args.get("action", "")
        value = args.get("value", "")
        # `list` is the only action that takes an OPTIONAL keyword
        # filter — never a session_id. Don't leak the "current" default
        # into the filter slot (was producing "No sessions found
        # matching 'current'" when the agent omitted session_id).
        if action == "list":
            keyword = args.get("session_id", "") or args.get("keyword", "") or value
            content = "list" + (("\n" + keyword) if keyword and keyword.lower() != "current" else "")
        else:
            sid = args.get("session_id", "current")
            content = action + "\n" + sid
            if value:
                content += "\n" + value
    elif tool_type == "manage_memory":
        action = args.get("action", "")
        if action == "add":
            text = args.get("text") or args.get("value") or args.get("content") or ""
            if not text and args.get("key"):
                text = str(args.get("key") or "")
            content = "add\n" + str(text)
            if args.get("category"):
                content += "\n" + args["category"]
            elif args.get("key"):
                content += "\n" + str(args["key"])
        elif action == "edit":
            content = "edit\n" + args.get("memory_id", "") + "\n" + args.get("text", "")
        elif action == "delete":
            content = "delete\n" + args.get("memory_id", "")
        elif action == "search":
            content = "search\n" + (args.get("text") or args.get("tex") or args.get("query") or "")
        elif action == "list":
            content = "list"
            if args.get("category"):
                content += "\n" + args["category"]
        else:
            content = action
    elif tool_type == "list_models":
        content = args.get("filter", "")
    elif tool_type == "ui_control":
        action = args.get("action", "")
        name = args.get("name", "")
        value = args.get("value", "")
        if action == "toggle":
            content = f"toggle {name} {value}"
        elif action == "open_panel":
            content = f"open_panel {name or value}"
        elif action == "open_email_reply":
            uid = args.get("uid") or name
            folder = args.get("folder") or value or "INBOX"
            mode = args.get("mode") or "reply"
            content = f"open_email_reply {uid} {folder} {mode}"
            body = args.get("body") or args.get("extra") or args.get("content") or ""
            if body:
                content += f" {body}"
        elif action == "set_mode":
            content = f"set_mode {value or name}"
        elif action == "switch_model":
            content = f"switch_model {value or name}"
        elif action == "set_theme":
            content = f"set_theme {value or name}"
        elif action == "create_theme":
            colors = args.get("colors", {})
            theme_name = name or value or "custom"
            bg = colors.get("bg", "#282c34")
            fg = colors.get("fg", "#9cdef2")
            panel = colors.get("panel", "#111111")
            border = colors.get("border", "#355a66")
            accent = colors.get("accent", "#e06c75")
            content = f"create_theme {theme_name} {bg} {fg} {panel} {border} {accent}"
            # Append advanced overrides as key=value
            adv_keys = [
                "userBubbleBg", "aiBubbleBg", "bubbleBorder", "sidebarBg",
                "sectionAccent", "brandColor", "inputBg", "inputBorder",
                "sendBtnBg", "sendBtnHover", "codeBg", "codeFg",
                "toggleBg", "toggleActive", "accentPrimary", "accentError",
            ]
            for ak in adv_keys:
                if colors.get(ak):
                    content += f" {ak}={colors[ak]}"
        else:
            content = action
    elif tool_type in ("manage_tasks", "manage_skills", "api_call",
                        "manage_endpoints", "manage_mcp", "manage_webhooks",
                        "manage_tokens", "manage_documents", "manage_settings"):
        content = json.dumps(args)
    elif tool_type == "ask_teacher":
        content = args.get("model", "auto") + "\n" + args.get("problem", "")
    elif tool_type == "ask_user":
        # Keep user-facing labels readable in the tool trace.  The outer SSE
        # JSON encoder will escape them for transport and JSON.parse restores
        # them once; pre-escaping here caused literal ``\u00f1`` sequences to
        # remain visible in the debug panel.
        content = json.dumps(args, ensure_ascii=False)
    else:
        content = json.dumps(args)

    return ToolBlock(tool_type, content)

# ---------------------------------------------------------------------------
# Argument validation and bounded repair (CALL-02 / CALL-03)
# ---------------------------------------------------------------------------
#
# validate_tool_arguments() checks a fully-parsed `arguments` dict against
# the matching entry in FUNCTION_TOOL_SCHEMAS (top-level properties only —
# it does not recurse into nested object/array schemas). repair_tool_arguments()
# then fixes only the *form* of a value the schema already accepts in
# principle (a number sent as its exact string form); it never invents a
# missing required field, drops/renames a key, or touches a path/scope
# value — a path error found by validate_tool_arguments is never
# "repaired away".

# Fields marked here as path-scoped are contractually confined to an
# allowed workspace/root by their tool's docstring (see FUNCTION_TOOL_SCHEMAS
# above) — "inside the allowed workspace", "existing authorized folder",
# "confined to the allowed roots". A `..` traversal segment or an absolute
# path is out of scope for these. Deliberately NOT included: read_file,
# write_file, edit_file, apply_patch and the folder/file-manager tools —
# their own schemas explicitly allow "any real path" (absolute, outside a
# single workspace), so an absolute path there is a legitimate argument, not
# a scope violation; those tools are confined at execution time instead, by
# `_resolve_tool_path`'s allowlist (src/tool_execution.py), which is out of
# this module's reach (it needs live filesystem/workspace state, not just
# the static schema).
PATH_ARGUMENT_FIELDS = {
    "plan_media_transform": {"source", "path"},
    "transform_media": {"source", "path"},
    "inspect_media": {"path"},
    "grep": {"path"},
    "glob": {"path"},
    "ls": {"path"},
    "find_symbol": {"path"},
    "callers": {"path"},
    "tests_for": {"path"},
    "rename_symbol": {"path"},
    "install_dependencies": {"project_root"},
    "manage_scripts": {"cwd"},
    "browser_extract": {"sandbox_root"},
}

_JSON_SCALAR_TYPES = {
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _schema_for_tool(tool_name: str) -> Optional[dict]:
    """Look up a tool's `parameters` JSON schema by name. Returns None for a
    tool not in FUNCTION_TOOL_SCHEMAS (nothing to validate against — callers
    should treat that as "no errors", not as a validation failure of its
    own: an unknown tool name is caught earlier in the call pipeline)."""
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") or {}
        if fn.get("name") == tool_name:
            return fn.get("parameters") or {}
    return None


def _type_matches(value: Any, expected: str) -> bool:
    """JSON-Schema type check. `bool` is deliberately excluded from
    "integer"/"number" — Python's `isinstance(True, int)` is True, but a
    boolean is never an acceptable substitute for a numeric argument."""
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    scalar = _JSON_SCALAR_TYPES.get(expected)
    if scalar is None:
        return True  # unknown/unlisted type keyword: nothing to check
    return isinstance(value, scalar)


def _path_out_of_scope(value: Any) -> bool:
    """A path-scoped argument may not escape its tool's workspace: no `..`
    traversal segment, and not an absolute path (POSIX `/...` or a
    `C:\\...`-style drive path)."""
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("/") or value.startswith("\\"):
        return True
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return True
    return ".." in value.replace("\\", "/").split("/")


@dataclass
class ArgumentError:
    """One localized problem found by validate_tool_arguments().

    `field` is the argument's JSON path (currently always a bare top-level
    key — see the module note on nesting). `kind` is one of "unknown_field",
    "wrong_type", "enum", "range", "path_scope", "missing_required". `detail`
    is a human-readable "what was seen" message; `seen` carries the raw
    offending value for a caller that wants to render it itself.
    """

    field: str
    kind: str
    detail: str
    seen: Any = None

    def __str__(self) -> str:
        return f"{self.field}: {self.detail}"


def validate_tool_arguments(tool_name: str, args: Any) -> List[ArgumentError]:
    """Validate a fully-parsed tool-call `arguments` object against its
    schema in FUNCTION_TOOL_SCHEMAS. Checks (CALL-02): wrong type, unknown
    field, enum value out of range, a numeric/string/array value outside its
    schema's `minimum`/`maximum`/`minLength`/`maxLength`/`minItems`/
    `maxItems` (see `_range_violation`), and — for fields FUNCTION_TOOL_SCHEMAS
    marks in PATH_ARGUMENT_FIELDS as path-scoped — a `..` traversal or an
    absolute path. Does not execute or resolve anything; a pure, read-only
    check usable regardless of how `args` was produced (native function
    call, repaired text, ToolCallAssembler's parsed_arguments, ...).
    """
    errors: List[ArgumentError] = []
    schema = _schema_for_tool(tool_name)
    if schema is None:
        return errors
    if not isinstance(args, dict):
        errors.append(ArgumentError("$", "wrong_type", f"expected an object, saw {type(args).__name__}", args))
        return errors

    properties = schema.get("properties") or {}
    for required in schema.get("required") or []:
        if required not in args:
            errors.append(ArgumentError(required, "missing_required", "required field is missing"))

    path_fields = PATH_ARGUMENT_FIELDS.get(tool_name, frozenset())
    for key, value in args.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            errors.append(ArgumentError(key, "unknown_field", f"not declared in the {tool_name} schema", value))
            continue

        expected_types = prop_schema.get("type")
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if expected_types and not any(_type_matches(value, t) for t in expected_types):
            errors.append(ArgumentError(
                key, "wrong_type",
                f"expected {'/'.join(expected_types)}, saw {type(value).__name__} ({value!r})",
                value,
            ))
            continue  # a value of the wrong type can't be enum/path-checked meaningfully

        enum = prop_schema.get("enum")
        if enum is not None and value not in enum:
            errors.append(ArgumentError(key, "enum", f"{value!r} is not one of {enum}", value))

        range_error = _range_violation(key, value, prop_schema)
        if range_error is not None:
            errors.append(range_error)

        if key in path_fields and _path_out_of_scope(value):
            errors.append(ArgumentError(key, "path_scope", f"{value!r} escapes the allowed scope (.. or absolute)", value))

    return errors


def _range_violation(key: str, value: Any, prop_schema: dict) -> Optional["ArgumentError"]:
    """CALL-02: the "rangos" half of a strict-types-and-ranges validator —
    `minimum`/`maximum` (numbers), `minLength`/`maxLength` (strings) and
    `minItems`/`maxItems` (arrays), all already declared on several schemas
    (e.g. `quality`'s 1-100, `line_count`'s 1-500) but never actually
    enforced here before now: a call with `quality: 9999` passed this
    validator clean and only failed once the tool itself rejected it, or
    silently clamped. `bool` is excluded from the numeric check the same way
    `_type_matches` excludes it from "integer"/"number" — a boolean is never
    a meaningful magnitude to bound. Deliberately not offered to
    `repair_tool_arguments`: clamping a value into range changes what the
    caller asked for, which is exactly what bounded repair (CALL-03) must
    never do.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = prop_schema.get("minimum")
        maximum = prop_schema.get("maximum")
        if minimum is not None and value < minimum:
            return ArgumentError(key, "range", f"{value!r} is below the minimum {minimum!r}", value)
        if maximum is not None and value > maximum:
            return ArgumentError(key, "range", f"{value!r} is above the maximum {maximum!r}", value)
    elif isinstance(value, str):
        min_len = prop_schema.get("minLength")
        max_len = prop_schema.get("maxLength")
        if min_len is not None and len(value) < min_len:
            return ArgumentError(key, "range", f"length {len(value)} is below minLength {min_len!r}", value)
        if max_len is not None and len(value) > max_len:
            return ArgumentError(key, "range", f"length {len(value)} is above maxLength {max_len!r}", value)
    elif isinstance(value, list):
        min_items = prop_schema.get("minItems")
        max_items = prop_schema.get("maxItems")
        if min_items is not None and len(value) < min_items:
            return ArgumentError(key, "range", f"{len(value)} item(s) is below minItems {min_items!r}", value)
        if max_items is not None and len(value) > max_items:
            return ArgumentError(key, "range", f"{len(value)} item(s) is above maxItems {max_items!r}", value)
    return None


def repair_tool_arguments(
    tool_name: str, args: Dict[str, Any], errors: List[ArgumentError]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Bounded repair (CALL-03): fix only the *form* of a value the schema
    already accepts, never its meaning. That means exactly two things: a
    number sent as a string that is an exact textual representation of that
    number ("90" -> 90 for an integer field, "1.5" -> 1.5 for a number
    field), and a boolean sent as the strings "true"/"false" (any case) for
    a boolean field. Both are shapes local models emit routinely
    (`"ignore_case": "true"`), and both are unambiguous; refusing them in
    strict mode would block calls that every tool used to accept.

    Never: adds a missing required field, drops or renames a key, changes
    which tool is being called, or touches a value flagged as a path/scope
    problem — repair_tool_arguments MUST NOT remove a `path_scope` error;
    the caller decides whether to surface original-vs-corrected to the
    model/user, this function only ever narrows toward "same meaning,
    stricter shape".

    Returns (repaired_args, applied_repairs) where `applied_repairs` is a
    list of `{"field", "from", "to", "reason"}` describing each change made,
    so a caller can show "original -> correction". `args` itself is never
    mutated; a shallow copy is returned even when no repair applies.
    """
    repaired = dict(args)
    applied: List[Dict[str, Any]] = []
    schema = _schema_for_tool(tool_name)
    if schema is None:
        return repaired, applied

    properties = schema.get("properties") or {}
    path_fields = PATH_ARGUMENT_FIELDS.get(tool_name, frozenset())
    fixable_kinds = {"wrong_type", "enum"}
    for err in errors:
        if err.kind not in fixable_kinds or err.field in path_fields:
            continue
        prop_schema = properties.get(err.field)
        if prop_schema is None or err.field not in repaired:
            continue
        if err.kind == "enum":
            # `"Week"` for an enum that says "week": the same word, one
            # spelling. Only an exact case-insensitive match of ONE member
            # qualifies; anything else is a different value and stays wrong.
            value = repaired[err.field]
            members = prop_schema.get("enum") or []
            if isinstance(value, str):
                hits = [m for m in members if isinstance(m, str) and m.lower() == value.strip().lower()]
                if len(hits) == 1 and hits[0] != value:
                    repaired[err.field] = hits[0]
                    applied.append({
                        "field": err.field, "from": value, "to": hits[0],
                        "reason": "enum member matched case-insensitively",
                    })
            continue
        expected_types = prop_schema.get("type")
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not expected_types:
            continue
        value = repaired[err.field]
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        new_value = None
        reason = "numeric string coerced to the schema's declared number type"
        if "boolean" in expected_types and stripped.lower() in ("true", "false"):
            new_value = stripped.lower() == "true"
            reason = "boolean string coerced to the schema's declared boolean type"
        if new_value is None and "integer" in expected_types:
            try:
                candidate = int(stripped)
            except ValueError:
                candidate = None
            if candidate is not None and str(candidate) == stripped:
                new_value = candidate
        if new_value is None and "number" in expected_types:
            try:
                candidate = float(stripped)
            except ValueError:
                candidate = None
            # Exact round-trip only: "1.5" -> 1.5, but not "1.50" or "1e0",
            # which read back differently from how they were written and so
            # are not an unambiguous "this string IS that number".
            if candidate is not None and str(candidate) == stripped:
                new_value = candidate
        if new_value is not None:
            repaired[err.field] = new_value
            applied.append({
                "field": err.field,
                "from": value,
                "to": new_value,
                "reason": reason,
            })

    return repaired, applied
