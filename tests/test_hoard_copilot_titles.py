"""Native Hoard project titles with punctuation link to a Faustus project; only the sidebar label is cleaned."""
import pytest

from services.hoard_copilot import HoardCopilotStore, sidebar_label
from services.projects import _NAME_RE, ProjectStore


def context(external_id, title=None, space="live", app="writer"):
    project = {"external_project_id": external_id}
    if title is not None:
        project["title"] = title
    return {"origin": {"app_id": app, "data_space_id": space, "surface": "editor"}, "project": project}


@pytest.fixture
def store(tmp_path):
    return HoardCopilotStore(str(tmp_path / "copilot.db"), ProjectStore(str(tmp_path / "projects.json")))


TITLES = [
    ("API: /article demo", "API article demo"),
    ('Quote "x"?', "Quote x"),
    ("a|b*c<d>", "a b c d"),
    ("tab\there\nnew line", "tab here new line"),
    ("C:\\drafts\\chapter 3", "C drafts chapter 3"),
]


@pytest.mark.parametrize("title,label", TITLES)
def test_titles_with_punctuation_create_a_valid_project(store, title, label):
    resolved = store.resolve_project(context("p-" + label, title), "owner")
    project = store.project_store().get(resolved["project_id"], "owner")
    assert project["name"] == label
    assert _NAME_RE.match(project["name"]) and _NAME_RE.match(project["folder"])
    # The context keeps the native title and the external id exactly as the app sent them.
    assert resolved["context"]["project"]["title"] == title
    assert resolved["context"]["project"]["external_project_id"] == "p-" + label


def test_a_url_used_as_id_without_title_links(store):
    external_id = "https://example.test/articles/42?view=full"
    first = store.resolve_project(context(external_id), "owner")
    project = store.project_store().get(first["project_id"], "owner")
    assert _NAME_RE.match(project["name"]) and _NAME_RE.match(project["folder"])
    assert first["context"]["project"]["external_project_id"] == external_id
    again = store.resolve_project(context(external_id), "owner")
    assert (again["project_id"], again["project_link_id"]) == (first["project_id"], first["project_link_id"])


def test_repeated_punctuated_title_reuses_the_link(store):
    first = store.resolve_project(context("x1", "API: /article demo"), "owner")
    second = store.resolve_project(context("x1", "API: /article demo"), "owner")
    assert (first["project_id"], first["project_link_id"]) == (second["project_id"], second["project_link_id"])
    assert len(store.project_store().list("owner")) == 1


def test_titles_that_clean_to_the_same_label_stay_separate(store):
    one = store.resolve_project(context("x1", "API: demo"), "owner")
    two = store.resolve_project(context("x2", "API/ demo"), "owner")
    three = store.resolve_project(context("x1", "API: demo", space="qa"), "owner")
    assert len({one["project_id"], two["project_id"], three["project_id"]}) == 3
    folders = {row["folder"] for row in store.project_store().list("owner")}
    assert len(folders) == 3


def test_a_title_made_only_of_forbidden_characters_falls_back(store):
    resolved = store.resolve_project(context("x1", '???:::"'), "owner")
    assert store.project_store().get(resolved["project_id"], "owner")["name"] == "Untitled"


def test_sidebar_label_limits_and_collapses():
    assert sidebar_label("a" * 200) == "a" * 80
    assert sidebar_label("  a  :  b  ") == "a b"
    assert sidebar_label(None) == "Untitled"
    assert sidebar_label("Título: «ñandú»") == "Título «ñandú»"
