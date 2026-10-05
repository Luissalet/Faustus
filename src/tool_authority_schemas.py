"""Schema text of the tools whose spec the tool authority owns.

Pure data: the model-facing description and JSON-schema parameters of each
migrated tool. `src.tool_authority_specs` registers them and
`src.tool_schemas.FUNCTION_TOOL_SCHEMAS` is generated from that registry, so
this is the only place these definitions are written down.
"""

CORE_SCHEMAS = {
    'bash': {'description': 'Run a shell command (full access). Prefer a dedicated tool whenever one fits the job '
                    '(reading, writing, editing, searching, or listing files); use bash only for what no '
                    'dedicated tool covers (installs, git, builds, running programs, system info). Do NOT create '
                    'or edit files via bash redirects/heredocs/sed -- use the dedicated file tools.',
     'parameters': {'type': 'object',
                    'properties': {'command': {'type': 'string', 'description': 'The shell command to execute'}},
                    'required': ['command']}},
    'python': {'description': 'Execute Python code to compute a result or test something. Prefer a dedicated tool whenever '
                    'one fits the job (reading, writing, or searching files); use python only for computation, '
                    'data processing, or scripting no dedicated tool covers.',
     'parameters': {'type': 'object',
                    'properties': {'code': {'type': 'string', 'description': 'Python code to execute'}},
                    'required': ['code']}},
    'powershell': {'description': 'Run a PowerShell script on this Windows host (pwsh or Windows PowerShell), starting in the '
                    'workspace. Use it for anything Windows-native: .bat/.cmd launchers, winget/choco, '
                    'Start-Process, services, registry, WMI, paths with backslashes. Write the script exactly as '
                    'you would type it in a PowerShell window — no outer quoting, no `powershell -Command`. A '
                    '.bat/.cmd runs with `& cmd.exe /c "thing.bat"`; `-File` only takes .ps1. `bash` on Windows '
                    'is Git Bash (POSIX syntax) and refuses to launch powershell/cmd for you. Not for creating '
                    'or editing files — use the file tools.',
     'parameters': {'type': 'object',
                    'properties': {'script': {'type': 'string',
                                              'description': 'PowerShell script to run (multi-line allowed)'}},
                    'required': ['script']}},
    'web_search': {'description': "Quick single web lookup for a fact or current event mid-task. NOT for 'research X' / 'do "
                    "research on X' — those are deep-research jobs; use trigger_research instead.",
     'parameters': {'type': 'object',
                    'properties': {'query': {'type': 'string', 'description': 'Search query'},
                                   'time_filter': {'type': 'string',
                                                   'enum': ['day', 'week', 'month', 'year'],
                                                   'description': 'Optional freshness filter for '
                                                                  'news/latest/today queries'}},
                    'required': ['query']}},
    'web_fetch': {'description': "Fetch and read the text content of a specific URL the user names (e.g. 'check example.com', "
                    "'what's on this page <url>'). Use when you already have a concrete URL/domain. NOT for "
                    "open-ended searches (use web_search) or 'research X' jobs (use trigger_research). Downloads "
                    "are size-budgeted; a '[partial content: ...]' notice in the result means the body was cut "
                    'short and you can re-call with full=true for the rest. Set inspect=true for a passive HTTP '
                    'profile (status, final URL and selected headers), without extra scans or security verdicts.',
     'parameters': {'type': 'object',
                    'properties': {'url': {'type': 'string',
                                           'description': 'The URL or domain to fetch (http/https; a bare domain '
                                                          'like example.com is fine)'},
                                   'inspect': {'type': 'boolean',
                                               'description': 'Return passive HTTP observations instead of page text.'},
                                   'full': {'type': 'boolean',
                                            'description': 'Raise the download budget to the hard cap for large '
                                                           'pages/files. Use only after a result reported '
                                                           'partial content.'}},
                    'required': ['url']}},
    'read_file': {'description': 'Read a file from disk. Optionally read a line range with offset/limit for large files. An '
                    'image file (png, jpg, gif, webp) comes back as the picture itself, so this is how you look '
                    'at a chart or image you produced (desktop_screenshot shows the screen, not a file). A PDF '
                    '(.pdf) or an Office/EPUB document (.docx, .pptx, .xlsx, .epub) comes back as its text, page '
                    "by page: read it with this, no script needed. The result carries a `revision` (the file's "
                    'current content hash) — pass it back as `base_revision` to write_file/edit_file/apply_patch '
                    'so the edit is refused instead of silently applied if the file changed since this read.',
     'parameters': {'type': 'object',
                    'properties': {'path': {'type': 'string', 'description': 'File path to read'},
                                   'offset': {'type': 'integer',
                                              'description': '1-based line to start reading from (optional)'},
                                   'limit': {'type': 'integer',
                                             'description': 'Max number of lines to read from offset '
                                                            '(optional)'}},
                    'required': ['path']}},
    'grep': {'description': 'Search file contents for a regular expression across a directory tree (uses ripgrep when '
                    'available, respecting .gitignore). Returns file:line:match. PREFER this over `bash grep/rg` '
                    'for code search — confined to the allowed roots, structured output.',
     'parameters': {'type': 'object',
                    'properties': {'pattern': {'type': 'string',
                                               'description': 'Regular expression to search for'},
                                   'path': {'type': 'string',
                                            'description': 'Directory or file to search (optional; defaults to '
                                                           'the project root)'},
                                   'glob': {'type': 'string',
                                            'description': "Only search files matching this glob, e.g. '*.py' "
                                                           '(optional)'},
                                   'ignore_case': {'type': 'boolean',
                                                   'description': 'Case-insensitive match (optional)'},
                                   'max_results': {'type': 'integer',
                                                   'description': 'Max matches to return (optional)'}},
                    'required': ['pattern']}},
    'glob': {'description': "Find files by glob pattern (recursive), newest first. e.g. '**/*.py'. PREFER this over "
                    '`bash find/ls` for locating files — confined to the allowed roots.',
     'parameters': {'type': 'object',
                    'properties': {'pattern': {'type': 'string',
                                               'description': "Glob pattern, e.g. '**/*.ts' or "
                                                              "'src/**/test_*.py'"},
                                   'path': {'type': 'string',
                                            'description': 'Base directory (optional; defaults to the project '
                                                           'root)'}},
                    'required': ['pattern']}},
    'ls': {'description': 'List the entries of a directory (folders first, then files with sizes). PREFER this over '
                    '`bash ls` — confined to the allowed roots.',
     'parameters': {'type': 'object',
                    'properties': {'path': {'type': 'string',
                                            'description': 'Directory to list (optional; defaults to the project '
                                                           'root)'}},
                    'required': []}},
    'get_workspace': {'description': 'Return the absolute path of the active workspace folder the user is working in. File tools '
                    'are confined to it; the shell starts there but is not sandboxed. Call this first when the '
                    "user refers to 'the project'/'the code'/'this folder' without a path, instead of asking "
                    'them. Takes no arguments.',
     'parameters': {'type': 'object', 'properties': {}, 'required': []}},
    'write_file': {'description': 'Write/save a file to disk',
     'parameters': {'type': 'object',
                    'properties': {'path': {'type': 'string', 'description': 'File path to write to'},
                                   'content': {'type': 'string', 'description': 'File content to write'},
                                   'base_revision': {'type': 'string',
                                                     'description': 'Optional: the `revision` a prior read_file '
                                                                    'of this exact path returned. If the file '
                                                                    'changed since then, the write is refused '
                                                                    'instead of overwriting the newer content.'}},
                    'required': ['path', 'content']}},
    'edit_file': {'description': 'Edit a file ON DISK by exact string replacement (home folder, project files, any real path '
                    'like ~/sweden.txt or /path/to/file). This is the right tool for files on disk — NOT '
                    "edit_document (that's for editor-panel documents). PREFER this over bash (sed/echo) — it "
                    'shows a diff. old_string must match the file exactly and be unique (or set replace_all). '
                    'Use write_file to create a new file.',
     'parameters': {'type': 'object',
                    'properties': {'path': {'type': 'string', 'description': 'File path to edit'},
                                   'old_string': {'type': 'string',
                                                  'description': 'Exact text to replace (must match the file, '
                                                                 'including indentation)'},
                                   'new_string': {'type': 'string', 'description': 'Replacement text'},
                                   'replace_all': {'type': 'boolean',
                                                   'description': 'Replace all occurrences instead of requiring '
                                                                  'a unique match'},
                                   'base_revision': {'type': 'string',
                                                     'description': 'Optional: the `revision` a prior read_file '
                                                                    'of this exact path returned. If the file '
                                                                    'changed since then, the edit is refused '
                                                                    'instead of applying on top of the newer '
                                                                    'content.'}},
                    'required': ['path', 'old_string', 'new_string']}},
    'apply_patch': {'description': 'Apply a multi-file source-code patch to disk. Use for real project files in the workspace '
                    'when several edits belong together. Patch must use *** Begin Patch / *** End Patch with Add '
                    'File, Update File, or Delete File sections. Prefer this over bash redirects/heredocs/sed.',
     'parameters': {'type': 'object',
                    'properties': {'patch_text': {'type': 'string',
                                                  'description': 'Patch text beginning with *** Begin Patch and '
                                                                 'ending with *** End Patch'},
                                   'base_revision': {'type': 'string',
                                                     'description': 'Optional: the `revision` a prior read_file '
                                                                    'returned for the file(s) this patch updates '
                                                                    'or deletes. Anchors every Update/Delete '
                                                                    'section in the patch — if any of those '
                                                                    'files changed since, the whole patch is '
                                                                    'refused before anything is touched. Not '
                                                                    'meaningful for a pure Add File.'}},
                    'required': ['patch_text']}},
    'plan_media_transform': {'description': 'Read-only preflight of a fixed media recipe: convert/resize single-frame images to '
                    'PNG/JPEG/WebP or extract first audio track to WAV/MP3 (stereo 48 kHz, max 10 minutes). '
                    'Measures input, reports losses and engine requirements. Does not write, install or run a '
                    'conversion. Original always preserved. path must be a new filename in an existing workspace '
                    'folder.',
     'parameters': {'type': 'object',
                    'properties': {'source': {'type': 'string', 'description': 'Local authorized source file'},
                                   'path': {'type': 'string',
                                            'description': 'Proposed NEW output filename with matching '
                                                           'extension'},
                                   'format': {'type': 'string', 'enum': ['png', 'jpeg', 'webp', 'wav', 'mp3']},
                                   'max_width': {'type': 'integer', 'minimum': 1, 'maximum': 8192},
                                   'max_height': {'type': 'integer', 'minimum': 1, 'maximum': 8192},
                                   'quality': {'type': 'integer',
                                               'minimum': 1,
                                               'maximum': 100,
                                               'description': 'JPEG/WebP only, default 90; not a filesize '
                                                              'target'},
                                   'background': {'type': 'string',
                                                  'pattern': '^#[0-9a-fA-F]{6}$',
                                                  'description': 'JPEG only: explicit background for images with '
                                                                 'alpha'}},
                    'required': ['source', 'path', 'format'],
                    'additionalProperties': False}},
    'transform_media': {'description': 'Execute a fixed local media recipe to a NEW workspace file; never overwrite original or '
                    'existing output. Images: PNG/JPEG/WebP, max 16MP, preserves aspect/no upscale, rejects '
                    'animation/multipage, strips metadata; JPEG alpha needs explicit background. Audio/video: '
                    'extract first audio track to WAV 16-bit or MP3 192k, stereo 48kHz, up to 10 minutes, FFmpeg '
                    'required. Max input 256MiB/output 128MiB, bounded runtime, cancellable with progress. '
                    'Returns validated output and hashes; no installs or AI calls. Preflight with '
                    'plan_media_transform. Not video encoding, subtitles, color-managed print or target-filesize '
                    'compression.',
     'parameters': {'type': 'object',
                    'properties': {'source': {'type': 'string', 'description': 'Local authorized source file'},
                                   'path': {'type': 'string',
                                            'description': 'NEW output filename in an existing authorized '
                                                           'folder; matching extension'},
                                   'format': {'type': 'string', 'enum': ['png', 'jpeg', 'webp', 'wav', 'mp3']},
                                   'max_width': {'type': 'integer', 'minimum': 1, 'maximum': 8192},
                                   'max_height': {'type': 'integer', 'minimum': 1, 'maximum': 8192},
                                   'quality': {'type': 'integer',
                                               'minimum': 1,
                                               'maximum': 100,
                                               'description': 'JPEG/WebP only, default 90'},
                                   'background': {'type': 'string',
                                                  'pattern': '^#[0-9a-fA-F]{6}$',
                                                  'description': 'JPEG only: explicit background for images with '
                                                                 'alpha'}},
                    'required': ['source', 'path', 'format'],
                    'additionalProperties': False}},
    'inspect_media': {'description': 'Inspect a local image, audio or video before editing/converting it. Returns measured '
                    'dimensions, display orientation, format, duration and stream metadata when available. '
                    'Read-only, workspace-confined; no model call, network fetch, transcription or generation. '
                    'Images and PCM WAV work locally; compressed audio/video require FFprobe. Missing metadata '
                    'is not guessed.',
     'parameters': {'type': 'object',
                    'properties': {'path': {'type': 'string',
                                            'description': 'Local media file path inside the allowed workspace; '
                                                           'not a URL or playlist'}},
                    'required': ['path'],
                    'additionalProperties': False}},
    'image_job': {'description': 'Check, collect or cancel an existing Prospero image request after a timeout or restart. Use '
                    'the request_id returned by generate_image or edit_image. This never starts another render; '
                    'action=cancel stops a queued or running render of that request only.',
     'parameters': {'type': 'object',
                    'properties': {'request_id': {'type': 'string',
                                                  'description': 'Request ID returned by the earlier image '
                                                                 'operation'},
                                   'action': {'type': 'string',
                                              'enum': ['status', 'cancel'],
                                              'description': 'status (default) checks or collects; cancel stops '
                                                             "this request's render"}},
                    'required': ['request_id']}},
}
