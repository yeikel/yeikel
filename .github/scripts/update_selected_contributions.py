#!/usr/bin/env python3
"""Refresh the generated contribution and skills sections in the profile README.

The updater uses PyGithub to find public, merged pull requests authored by the
selected GitHub user outside repositories owned by that user. It deduplicates
the search results, orders them by merge time, and selects the requested number
of recent contributions without allowing curated highlights to consume those
slots.

Curated highlights are loaded from ``.github/data/highlighted-contributions.json``.
They are rendered separately from the automatically selected recent work. The
script also derives a concise language skills table from contribution repository
metadata. Small dependency updates, typo corrections, explicitly excluded
contributions, and excluded languages are omitted only from the skills evidence.

The rendered contributions are grouped by repository. Both the contribution
list and skills table replace only their marker-delimited sections in the target
README. Missing or duplicated markers, malformed highlight data, unexpected
GitHub data, and invalid URLs fail before the README is written. The file is
written only when its generated content changes.

Inputs:

* The target README is the optional positional argument and defaults to
  ``README.md``.
* ``--username`` selects the GitHub user. It defaults to ``PROFILE_USERNAME``
  or ``GITHUB_REPOSITORY_OWNER``.
* ``--limit`` controls the number of automatic recent contributions and
  defaults to three.
* ``--highlights`` overrides the curated highlight JSON file.
* ``GITHUB_TOKEN`` enables authenticated API access. Public access is used when
  the variable is absent.

Run the locked project from the repository root with:

    uv run --project .github/scripts --locked \\
        python .github/scripts/update_selected_contributions.py README.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from github import Auth, Github
from github.GithubException import GithubException


START_MARKER = "<!-- selected-contributions:start -->"
END_MARKER = "<!-- selected-contributions:end -->"
GENERATED_NOTICE = (
    "<!-- This section is updated automatically by "
    ".github/workflows/update-selected-contributions.yml. -->"
)
SKILLS_START_MARKER = "<!-- contribution-skills:start -->"
SKILLS_END_MARKER = "<!-- contribution-skills:end -->"
SKILLS_GENERATED_NOTICE = (
    "<!-- This section is derived automatically from public contribution repositories. -->"
)
PER_PAGE = 100
MAX_SEARCH_RESULTS = 1_000
MAX_SKILL_REPOSITORIES = 100
MAX_SKILLS = 4
MAX_REPOSITORIES_PER_SKILL = 2
MAX_HIGHLIGHTED_SKILL_EXAMPLES = 2
MAX_SMALL_DEPENDENCY_UPDATE_FILES = 2
EXCLUDED_SKILL_LANGUAGES = frozenset({"Kotlin", "MDX"})
EXCLUDED_SKILL_CONTRIBUTIONS = frozenset(
    {("opencontainers/distribution-spec", 465)}
)
TYPO_CORRECTION_PATTERN = re.compile(
    r"\b(?:grammar|misspelling|spelling|typo|typos)\b",
    re.IGNORECASE,
)
DEPENDENCY_UPDATE_PATTERN = re.compile(
    r"\b(?:bump|upgrade|upgrades|update)\b.*"
    r"(?:\bfrom\b.+\bto\b|\bto\s+v?\d|\bversions?\b)",
    re.IGNORECASE,
)
DEFAULT_HIGHLIGHTS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "highlighted-contributions.json"
)
HighlightedContribution = tuple[str, int, str, str]
SkillRepository = tuple[str, str]


class Contribution(TypedDict):
    """Normalized GitHub contribution data used by the README formatters."""

    repository: str
    number: int
    title: str
    html_url: str
    merged_at: datetime


def load_highlighted_contributions(
    path: Path,
) -> tuple[HighlightedContribution, ...]:
    """Load and validate curated profile contributions from JSON."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RuntimeError(
            f"Unable to read highlighted contributions from {path}"
        ) from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid highlighted contributions JSON in {path}") from error

    if not isinstance(payload, list):
        raise RuntimeError("Highlighted contributions JSON must contain a list")

    contributions: list[HighlightedContribution] = []
    seen: set[tuple[str, int]] = set()
    required_fields = {"repository", "number", "title"}
    for index, item in enumerate(payload):
        entry_name = f"Highlighted contribution at index {index}"
        if not isinstance(item, dict) or set(item) != required_fields:
            raise RuntimeError(
                f"{entry_name} must contain exactly repository, number, and title"
            )

        repository = item["repository"]
        number = item["number"]
        title = item["title"]
        if (
            not isinstance(repository, str)
            or repository.count("/") != 1
            or any(
                not part or part.strip() != part
                for part in repository.split("/")
            )
        ):
            raise RuntimeError(f"{entry_name} has an invalid repository")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise RuntimeError(f"{entry_name} must have a positive integer number")
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError(f"{entry_name} must have a non-empty title")

        key = (repository, number)
        if key in seen:
            raise RuntimeError(
                f"Duplicate highlighted contribution: {repository}#{number}"
            )
        seen.add(key)
        contributions.append(
            (
                repository,
                number,
                title,
                f"https://github.com/{repository}/pull/{number}",
            )
        )

    return tuple(contributions)


def create_github_client(token: str | None) -> Github:
    """Create a PyGithub client for authenticated or public API access."""
    auth = Auth.Token(token) if token else None
    return Github(
        auth=auth,
        per_page=PER_PAGE,
        user_agent="yeikel-profile-readme-updater",
    )


def fetch_all_contributions(
    username: str,
    github_client: Github,
) -> list[Contribution]:
    """Return merged public PRs outside the user's repos, newest first."""
    query = f"is:pr author:{username} is:merged is:public -user:{username}"
    search_results = github_client.search_issues(
        query=query,
        sort="updated",
        order="desc",
    )
    contributions: list[Contribution] = []
    seen: set[tuple[str, int]] = set()
    for index, issue in enumerate(search_results):
        if index >= MAX_SEARCH_RESULTS:
            break

        pull_request = issue.pull_request
        if pull_request is None or pull_request.merged_at is None:
            continue
        repository = _repository_name(issue.repository_url)
        key = (repository, issue.number)
        if key in seen:
            continue
        seen.add(key)
        contributions.append(
            {
                "repository": repository,
                "number": issue.number,
                "title": issue.title,
                "html_url": pull_request.html_url or issue.html_url,
                "merged_at": pull_request.merged_at,
            }
        )

    contributions.sort(key=lambda item: item["merged_at"], reverse=True)
    if not contributions:
        raise RuntimeError("GitHub returned no merged public contributions")
    return contributions


def fetch_contributions(
    username: str,
    limit: int,
    github_client: Github,
) -> list[Contribution]:
    """Return the most recently merged public PRs outside the user's repos."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")

    return fetch_all_contributions(
        username=username,
        github_client=github_client,
    )[:limit]


def _repository_name(repository_url: str) -> str:
    prefix = "https://api.github.com/repos/"
    if not repository_url.startswith(prefix):
        raise RuntimeError(f"Unexpected repository URL: {repository_url}")
    name = repository_url.removeprefix(prefix).strip("/")
    if len(name.split("/")) != 2:
        raise RuntimeError(f"Unexpected repository URL: {repository_url}")
    return name


def _pull_request_url(item: Contribution) -> str:
    url = item["html_url"]
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.netloc != "github.com" or "/pull/" not in parsed.path:
        raise RuntimeError(f"Unexpected pull request URL: {url}")
    return str(url)


def _is_low_signal_skill_contribution(
    item: Contribution,
    *,
    github_client: Github,
) -> bool:
    repository = item["repository"]
    number = int(item["number"])
    if (repository, number) in EXCLUDED_SKILL_CONTRIBUTIONS:
        return True

    title = " ".join(str(item["title"]).split())
    if TYPO_CORRECTION_PATTERN.search(title):
        return True
    if not DEPENDENCY_UPDATE_PATTERN.search(title):
        return False

    try:
        changed_files = github_client.get_repo(repository).get_pull(number).changed_files
    except GithubException as error:
        if error.status in {404, 410}:
            return True
        raise
    if (
        not isinstance(changed_files, int)
        or isinstance(changed_files, bool)
        or changed_files < 0
    ):
        raise RuntimeError("GitHub returned an unexpected changed_files value")
    return changed_files <= MAX_SMALL_DEPENDENCY_UPDATE_FILES


def _escape_link_text(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def select_recent_contributions(
    items: list[Contribution],
    limit: int,
    highlighted_contributions: tuple[HighlightedContribution, ...] = (),
) -> list[Contribution]:
    """Select recent non-highlighted PRs so pinned entries do not consume slots."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")

    highlighted_keys = {
        (repository, number)
        for repository, number, _title, _url in highlighted_contributions
    }
    selected = []
    for item in items:
        key = (item["repository"], int(item["number"]))
        if key in highlighted_keys:
            continue
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


def fetch_contribution_skills(
    items: list[Contribution],
    github_client: Github,
    max_repositories: int = MAX_SKILL_REPOSITORIES,
    highlighted_contributions: tuple[HighlightedContribution, ...] = (),
) -> dict[str, list[SkillRepository]]:
    """Group repositories and meaningful contribution links by language."""
    if max_repositories < 1:
        raise ValueError("max_repositories must be positive")

    meaningful_contribution_urls: dict[str, str] = {}
    for repository, _number, _title, url in highlighted_contributions:
        meaningful_contribution_urls.setdefault(repository, url)

    repositories: list[str] = []
    for item in items:
        repository = item["repository"]
        if repository in repositories:
            continue
        if _is_low_signal_skill_contribution(
            item,
            github_client=github_client,
        ):
            continue
        repositories.append(repository)
        meaningful_contribution_urls.setdefault(repository, _pull_request_url(item))
        if len(repositories) >= max_repositories:
            break

    repositories_by_language: dict[str, list[SkillRepository]] = {}
    for repository in repositories:
        try:
            language = github_client.get_repo(repository).language
        except GithubException as error:
            if error.status in {404, 410}:
                continue
            raise

        if language is None:
            continue
        if not isinstance(language, str) or not language.strip():
            raise RuntimeError(f"GitHub returned an unexpected language for {repository}")
        if language in EXCLUDED_SKILL_LANGUAGES:
            continue
        repositories_by_language.setdefault(language, []).append(
            (repository, meaningful_contribution_urls[repository])
        )

    if not repositories_by_language:
        raise RuntimeError("GitHub returned no repository language metadata")

    ranked_languages = sorted(
        repositories_by_language.items(),
        key=lambda entry: -len(entry[1]),
    )
    return dict(ranked_languages)


def format_skills(
    repositories_by_language: dict[str, list[SkillRepository]],
    max_skills: int = MAX_SKILLS,
    max_repositories_per_skill: int = MAX_REPOSITORIES_PER_SKILL,
    highlighted_contributions: tuple[HighlightedContribution, ...] = (),
) -> str:
    """Render repository-backed language skills as Markdown."""
    if max_skills < 1 or max_repositories_per_skill < 1:
        raise ValueError("skill display limits must be positive")

    ranked_languages = sorted(
        repositories_by_language.items(),
        key=lambda entry: -len(entry[1]),
    )[:max_skills]
    if not ranked_languages:
        raise RuntimeError("No contribution skills are available to format")

    highlighted_by_repository: dict[str, list[HighlightedContribution]] = {}
    for contribution in highlighted_contributions:
        highlighted_by_repository.setdefault(contribution[0], []).append(contribution)

    lines = [
        "Primary languages from recent merged contributions, excluding minor dependency "
        "and typo fixes. Each repository links to a representative contribution.",
        "",
        "| Language | Contribution evidence |",
        "| --- | --- |",
    ]
    for language, repositories in ranked_languages:
        highlighted_repository = next(
            (
                repository
                for repository, _url in repositories
                if repository in highlighted_by_repository
            ),
            None,
        )
        if highlighted_repository:
            examples = highlighted_by_repository[highlighted_repository][
                : min(MAX_HIGHLIGHTED_SKILL_EXAMPLES, max_repositories_per_skill)
            ]
            example_links = ", ".join(
                f"[{repository}#{number}]({url})"
                for repository, number, _title, url in examples
            )
            other_repositories = [
                repository
                for repository in repositories
                if repository[0] != highlighted_repository
            ]
            remaining_slots = max_repositories_per_skill - len(examples)
            displayed_repositories = other_repositories[:remaining_slots]
            repository_links = f"Highlighted examples: {example_links}"
            if displayed_repositories:
                other_links = ", ".join(
                    f"[{repository}]({contribution_url})"
                    for repository, contribution_url in displayed_repositories
                )
                repository_links += f". Other repository evidence: {other_links}"
            additional_count = len(other_repositories) - len(displayed_repositories)
        else:
            displayed_repositories = repositories[:max_repositories_per_skill]
            repository_links = ", ".join(
                f"[{repository}]({contribution_url})"
                for repository, contribution_url in displayed_repositories
            )
            additional_count = len(repositories) - len(displayed_repositories)
        repository_word = "repository" if additional_count == 1 else "repositories"
        additional = (
            f" (+{additional_count} more {repository_word})"
            if additional_count
            else ""
        )
        lines.append(f"| **{language}** | {repository_links}{additional} |")
    return "\n".join(lines)


def format_contributions(
    items: list[Contribution],
    highlighted_contributions: tuple[HighlightedContribution, ...] = (),
) -> str:
    highlighted_by_repository: dict[str, list[str]] = {}
    recent_by_repository: dict[str, list[str]] = {}
    repository_order: list[str] = []
    highlighted_keys = set()

    for repository, number, title, url in highlighted_contributions:
        highlighted_keys.add((repository, number))
        if repository not in repository_order:
            repository_order.append(repository)
        highlighted_by_repository.setdefault(repository, []).append(
            f"- [#{number}: {_escape_link_text(title)}]({url})"
        )

    for item in items:
        repository = item["repository"]
        number = int(item["number"])
        if (repository, number) in highlighted_keys:
            continue
        if repository not in repository_order:
            repository_order.append(repository)
        title = _escape_link_text(str(item["title"]))
        url = _pull_request_url(item)
        recent_by_repository.setdefault(repository, []).append(
            f"- [#{number}: {title}]({url})"
        )

    lines = []
    for repository in repository_order:
        if lines:
            lines.append("")
        lines.append(f"### [{repository}](https://github.com/{repository})")
        highlighted = highlighted_by_repository.get(repository)
        if highlighted:
            lines.extend(["", "#### Highlights", ""])
            lines.extend(highlighted)
        recent = recent_by_repository.get(repository)
        if recent:
            lines.extend(["", "#### Recent contributions", ""])
            lines.extend(recent)
    return "\n".join(lines)


def _update_generated_section(
    document: str,
    *,
    start_marker: str,
    end_marker: str,
    notice: str,
    content: str,
    section_name: str,
) -> str:
    if document.count(start_marker) != 1 or document.count(end_marker) != 1:
        raise RuntimeError(
            f"README must contain exactly one {section_name} marker pair"
        )

    before, marker, remainder = document.partition(start_marker)
    _, found_end_marker, after = remainder.partition(end_marker)
    if not marker or not found_end_marker:
        raise RuntimeError(
            f"README {section_name} markers are missing or out of order"
        )

    generated = (
        f"{start_marker}\n"
        f"{notice}\n"
        f"{content}\n"
        f"{end_marker}"
    )
    return f"{before}{generated}{after}"


def update_readme_text(
    readme: str,
    contribution_list: str,
    skill_list: str | None = None,
) -> str:
    updated = _update_generated_section(
        readme,
        start_marker=START_MARKER,
        end_marker=END_MARKER,
        notice=GENERATED_NOTICE,
        content=contribution_list,
        section_name="contribution",
    )
    if skill_list is not None:
        updated = _update_generated_section(
            updated,
            start_marker=SKILLS_START_MARKER,
            end_marker=SKILLS_END_MARKER,
            notice=SKILLS_GENERATED_NOTICE,
            content=skill_list,
            section_name="skills",
        )
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("readme", nargs="?", type=Path, default=Path("README.md"))
    parser.add_argument(
        "--username",
        default=os.environ.get("PROFILE_USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER"),
        help="GitHub username whose contributions should be selected",
    )
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--highlights",
        type=Path,
        default=DEFAULT_HIGHLIGHTS_PATH,
        help="JSON file containing curated highlighted contributions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.username:
        raise SystemExit("--username or PROFILE_USERNAME is required")

    token = os.environ.get("GITHUB_TOKEN")
    highlighted_contributions = load_highlighted_contributions(args.highlights)
    with create_github_client(token) as github_client:
        all_contributions = fetch_all_contributions(
            username=args.username,
            github_client=github_client,
        )
        contributions = select_recent_contributions(
            all_contributions,
            args.limit,
            highlighted_contributions,
        )
        skills = fetch_contribution_skills(
            all_contributions,
            github_client=github_client,
            highlighted_contributions=highlighted_contributions,
        )
    current = args.readme.read_text(encoding="utf-8")
    updated = update_readme_text(
        current,
        format_contributions(contributions, highlighted_contributions),
        format_skills(
            skills,
            highlighted_contributions=highlighted_contributions,
        ),
    )

    if updated == current:
        print("Profile content is already current")
        return 0

    with args.readme.open("w", encoding="utf-8", newline="\n") as readme_file:
        readme_file.write(updated)
    print(f"Updated {args.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
