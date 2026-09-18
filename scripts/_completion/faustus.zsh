#compdef faustus faustus-backup faustus-calendar faustus-contacts faustus-cookbook faustus-docs faustus-gallery faustus-mail faustus-mcp faustus-memory faustus-notes faustus-personal faustus-preset faustus-research faustus-sessions faustus-signature faustus-skills faustus-tasks faustus-theme faustus-webhook
# Zsh tab-completion for the faustus umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/faustus-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `faustus <tab>` completes subcommands; `faustus mail <tab>`
# completes mail subcommands; `faustus-mail <tab>` works the same.

_faustus_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _faustus_subs

_faustus_refresh() {
    _faustus_subs=()
    local dir="$(_faustus_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/faustus-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#faustus-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _faustus_subs[$sub]="$commands"
    done
}

_faustus() {
    [[ ${#_faustus_subs} -eq 0 ]] && _faustus_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "faustus" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_faustus_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_faustus_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_faustus_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # faustus-foo <tab>
    local sub="${cmd#faustus-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_faustus_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_faustus "$@"
