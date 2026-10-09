"""Preserve the human objective across exact-approval resumes."""


def objective(original_request, exact_approval=None):
    saved = getattr(getattr(exact_approval, "pending", None), "continuation_query", None)
    return saved if isinstance(saved, str) and saved.strip() else str(original_request or "")
