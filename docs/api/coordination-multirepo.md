# Coordination tasks spanning repositories

The native coordination client resolves comma-separated task repository names
against the Owner-configured binding. Every constituent repository must be in
scope. Each path in a multi-repository task must begin with its repository name,
for example `HoardLink/hoard_link/hub/server.py` or `Faustus/src/coordination/client.py`.
An unqualified path such as `src/client.py` is ambiguous and returns a repairable
argument error. No repository is inferred from a partial permission match.

Single-repository tasks accept repository-relative paths and once-qualified
paths. Forward slashes and backslashes are normalized; absolute paths, drive
prefixes, dot/traversal segments and invalid resource characters remain rejected.
Claim and add derive their default locks from this same complete path validation;
no combined prefix or duplicate repository prefix is invented.

A historical repository whose actual name contains a comma remains a distinct
literal name when the binding lists that exact name and explicitly scopes its
literal resource namespace. A comma alias without that resource authority cannot
grant missing constituent repositories. The Owner's configured namespace is the
authority; no filesystem, account or execution permission is added by this parser.

Board project membership uses all repository names, and action hints use every
full path even when the preview truncates the list. A closed task inside scope
reports `task is already closed`. Global-context reading, required delivery,
ownership, revision checks, connector restrictions and read-only turns retain
their existing behavior. The ordinary API, MCP connector and Studio tool results
use the same native client.
