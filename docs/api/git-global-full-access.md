# Git approval and global Full access

Git tools consult the same normalized, persisted `tool_approval_mode()` preference as the outer tool approval gate. When that value is `full`, it supplies approval for the repo-policy fields that already permitted a human-confirmed override. A second `ask_user`, `user_confirmed` argument or exact approval card is unnecessary.

The shared check covers `commit` for `git_commit`, `use_branch` for branch creation, checkout, merge and local deletion, and `push` for push, publication and opening a pull request. It reads the approval preference once before dispatch and does not rewrite global or repository Git policy. Automatic before/after-turn Git behavior still follows its saved policy.

`ask`, `auto`, invalid values and unavailable settings retain the previous approval behavior. Tool/context arguments named `tool_approval_mode` cannot select Full access. Existing explicit human confirmations remain supported. No settings-writing tool or self-escalation path is added.

Full access supplies approval, not tool availability, workspace access, author identity, Git credentials or permission to ignore invalid refs/paths. Missing identity, disabled tools and invalid paths still fail. No push, publish or pull-request operation was sent to an external remote during verification.

Verification uses isolated repositories and a GPU-free HTTP model simulator through native API, REST MCP chat and the normal Studio compositor. It reads a file and calls `git_commit` with Git policy `commit=false`, without confirmation flags: the resulting local commit is checked directly in Git. Ask/Automatic do not commit without their existing approval. Tests exercise every overrideable field, invalid/failing settings, spoofed mode arguments and the unchanged validation boundaries. This evidence tests the approval integration, not the reasoning ability of a real model.
