---
id: rust/coding-style
title: Rust coding style
applies_to: [Rust]
priority: 32
summary: rustfmt output as-is, clippy clean, doc comments on public items.
---

- Run `rustfmt` and keep the diff clean against it — never hand-format.
- Keep `cargo clippy` clean; don't add `#[allow(...)]` to silence a real
  finding without a comment explaining why it's a false positive here.
- Write a doc comment (`///`) on every public item explaining what it does
  and any non-obvious invariant a caller must uphold.
- Prefer `impl Trait` in argument/return position over a generic parameter
  when the concrete type genuinely doesn't matter to the caller.
- Keep modules organised by feature/domain, not by kind (avoid a catch-all
  `utils.rs` that accumulates unrelated helpers).
