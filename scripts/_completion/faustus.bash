#!/usr/bin/env bash
# Tab-completion for the `faustus` umbrella + every `faustus-*` CLI.
#
# Source from your shell rc:
#     source /path/to/faustus-ui/scripts/_completion/faustus.bash
#
# Or wire it once per machine:
#     sudo install -m 644 faustus.bash /etc/bash_completion.d/faustus
#
# What it does:
#   - On the first word after `faustus`, complete with the list of
#     subcommands (`mail`, `calendar`, ...).
#   - On subsequent words, complete with the subcommand's first-token
#     subcommands (`list`, `show`, ...) which we cache by parsing the
#     tool's own --help output. Updates lazily; refresh by running
#     `_faustus_refresh_cache`.
#   - Same completion works for the individual `faustus-foo` scripts.

_faustus_scripts_dir() {
    # Resolve the scripts/ dir from the script that sources us. We assume
    # the user sourced the file directly out of scripts/_completion/.
    local self="${BASH_SOURCE[0]}"
    while [ -L "$self" ]; do self=$(readlink "$self"); done
    cd "$(dirname "$self")/.." && pwd
}

declare -A _FAUSTUS_SUBS_CACHE=()

_faustus_refresh_cache() {
    local dir="$(_faustus_scripts_dir)"
    _FAUSTUS_SUBS_CACHE=()
    # Prefer the project venv's Python so deps (bcrypt, sqlalchemy, ...)
    # resolve. Falls back to system `python3` for container installs.
    local py="$dir/../venv/bin/python"
    [ -x "$py" ] || py="$(command -v python3)"
    local f
    for f in "$dir"/faustus-*; do
        [ -x "$f" ] || continue
        case "$f" in *.bak|*.pyc|*.pre-*) continue ;; esac
        local name="$(basename "$f")"
        local sub="${name#faustus-}"
        local help_out
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        local commands
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _FAUSTUS_SUBS_CACHE[$sub]="$commands"
    done
}

_faustus_complete() {
    [ ${#_FAUSTUS_SUBS_CACHE[@]} -eq 0 ] && _faustus_refresh_cache

    local cur="${COMP_WORDS[COMP_CWORD]}"
    local cmd="${COMP_WORDS[0]}"

    # `faustus <tab>` → list every subcommand
    if [ "$cmd" = "faustus" ]; then
        if [ "$COMP_CWORD" -eq 1 ]; then
            local subs="${!_FAUSTUS_SUBS_CACHE[@]} help"
            COMPREPLY=($(compgen -W "$subs" -- "$cur"))
            return 0
        fi
        # `faustus foo <tab>` — complete with foo's own subcommands
        local sub="${COMP_WORDS[1]}"
        # `faustus help <tab>` lists every subcommand
        if [ "$sub" = "help" ] && [ "$COMP_CWORD" -eq 2 ]; then
            COMPREPLY=($(compgen -W "${!_FAUSTUS_SUBS_CACHE[*]}" -- "$cur"))
            return 0
        fi
        if [ "$COMP_CWORD" -eq 2 ]; then
            COMPREPLY=($(compgen -W "${_FAUSTUS_SUBS_CACHE[$sub]}" -- "$cur"))
            return 0
        fi
        return 0
    fi

    # Direct `faustus-foo <tab>` (no umbrella)
    local sub="${cmd#faustus-}"
    if [ "$COMP_CWORD" -eq 1 ]; then
        COMPREPLY=($(compgen -W "${_FAUSTUS_SUBS_CACHE[$sub]}" -- "$cur"))
        return 0
    fi
}

# Register the completion for every faustus-* script + the umbrella.
complete -F _faustus_complete faustus
for f in "$(_faustus_scripts_dir)"/faustus-*; do
    [ -x "$f" ] || continue
    case "$f" in *.bak|*.pyc|*.pre-*) continue ;; esac
    complete -F _faustus_complete "$(basename "$f")"
done
