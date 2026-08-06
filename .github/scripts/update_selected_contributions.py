#!/usr/bin/env python3
"""Update the generated contribution list in the profile README."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


API_URL = "https://api.github.com/search/issues"
API_VERSION = "2022-11-28"
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
MAX_SKILLS = 8
MAX_REPOSITORIES_PER_SKILL = 3
DEFAULT_HIGHLIGHTS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "highlighted-contributions.json"
)
HighlightedContribution = tuple[str, int, str, str]


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


def _request_json(
    *,
    url: str,
    username: str,
    token: str | None,
    open_url: Callable[..., Any],
) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{username}-profile-readme-updater",
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers)
    with open_url(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an unexpected API response")
    return payload


def _request_page(
    *,
    username: str,
    page: int,
    token: str | None,
    open_url: Callable[..., Any],
) -> dict[str, Any]:
    query = f"is:pr author:{username} is:merged is:public -user:{username}"
    url = f"{API_URL}?{urlencode({'q': query, 'sort': 'updated', 'order': 'desc', 'per_page': PER_PAGE, 'page': page})}"
    payload = _request_json(
        url=url,
        username=username,
        token=token,
        open_url=open_url,
    )

    if not isinstance(payload.get("items"), list):
        raise RuntimeError("GitHub returned an unexpected search response")
    if payload.get("incomplete_results") is True:
        raise RuntimeError("GitHub returned incomplete search results")
    return payload


def fetch_all_contributions(
    username: str,
    token: str | None = None,
    open_url: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Return merged public PRs outside the user's repos, newest first."""
    opener = open_url or urlopen
    items: list[dict[str, Any]] = []
    total_count = MAX_SEARCH_RESULTS

    for page in range(1, (MAX_SEARCH_RESULTS // PER_PAGE) + 1):
        payload = _request_page(
            username=username,
            page=page,
            token=token,
            open_url=opener,
        )
        page_items = payload["items"]
        items.extend(page_items)
        total_count = min(int(payload.get("total_count", len(items))), MAX_SEARCH_RESULTS)
        if len(items) >= total_count or not page_items:
            break

    merged_items = [
        item
        for item in items
        if isinstance(item.get("pull_request"), dict)
        and item["pull_request"].get("merged_at")
    ]
    merged_items.sort(
        key=lambda item: item["pull_request"]["merged_at"],
        reverse=True,
    )

    if not merged_items:
        raise RuntimeError("GitHub returned no merged public contributions")
    return merged_items


def fetch_contributions(
    username: str,
    limit: int,
    token: str | None = None,
    open_url: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the most recently merged public PRs outside the user's repos."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")

    return fetch_all_contributions(
        username=username,
        token=token,
        open_url=open_url,
    )[:limit]


def _repository_name(repository_url: str) -> str:
    prefix = "https://api.github.com/repos/"
    if not repository_url.startswith(prefix):
        raise RuntimeError(f"Unexpected repository URL: {repository_url}")
    name = repository_url.removeprefix(prefix).strip("/")
    if len(name.split("/")) != 2:
        raise RuntimeError(f"Unexpected repository URL: {repository_url}")
    return name


def _pull_request_url(item: dict[str, Any]) -> str:
    url = item["pull_request"].get("html_url") or item.get("html_url")
    parsed = urlparse(str(url))
    if parsed.scheme != "https" or parsed.netloc != "github.com" or "/pull/" not in parsed.path:
        raise RuntimeError(f"Unexpected pull request URL: {url}")
    return str(url)


def _escape_link_text(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def select_recent_contributions(
    items: list[dict[str, Any]],
    limit: int,
    highlighted_contributions: tuple[HighlightedContribution, ...] = (),
) -> list[dict[str, Any]]:
    """Select recent non-highlighted PRs so pinned entries do not consume slots."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")

    highlighted_keys = {
        (repository, number)
        for repository, number, _title, _url in highlighted_contributions
    }
    selected = []
    for item in items:
        key = (_repository_name(str(item["repository_url"])), int(item["number"]))
        if key in highlighted_keys:
            continue
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


def fetch_contribution_skills(
    items: list[dict[str, Any]],
    username: str,
    token: str | None = None,
    open_url: Callable[..., Any] | None = None,
    max_repositories: int = MAX_SKILL_REPOSITORIES,
) -> dict[str, list[str]]:
    """Group contributed repositories by their primary GitHub language."""
    if max_repositories < 1:
        raise ValueError("max_repositories must be positive")

    repository_urls: dict[str, str] = {}
    for item in items:
        repository_url = str(item["repository_url"])
        repository = _repository_name(repository_url)
        repository_urls.setdefault(repository, repository_url)
        if len(repository_urls) >= max_repositories:
            break

    opener = open_url or urlopen
    repositories_by_language: dict[str, list[str]] = {}
    for repository, repository_url in repository_urls.items():
        try:
            payload = _request_json(
                url=repository_url,
                username=username,
                token=token,
                open_url=opener,
            )
        except HTTPError as error:
            if error.code in {404, 410}:
                continue
            raise

        language = payload.get("language")
        if language is None:
            continue
        if not isinstance(language, str) or not language.strip():
            raise RuntimeError(f"GitHub returned an unexpected language for {repository}")
        repositories_by_language.setdefault(language, []).append(repository)

    if not repositories_by_language:
        raise RuntimeError("GitHub returned no repository language metadata")

    ranked_languages = sorted(
        repositories_by_language.items(),
        key=lambda entry: -len(entry[1]),
    )
    return dict(ranked_languages)


def format_skills(
    repositories_by_language: dict[str, list[str]],
    max_skills: int = MAX_SKILLS,
    max_repositories_per_skill: int = MAX_REPOSITORIES_PER_SKILL,
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

    lines = [
        "Primary languages across public repositories in my recent merged contribution history:",
        "",
    ]
    for language, repositories in ranked_languages:
        displayed_repositories = repositories[:max_repositories_per_skill]
        repository_links = ", ".join(
            f"[{repository}](https://github.com/{repository})"
            for repository in displayed_repositories
        )
        additional_count = len(repositories) - len(displayed_repositories)
        repository_word = "repository" if additional_count == 1 else "repositories"
        additional = (
            f" (+{additional_count} more {repository_word})"
            if additional_count
            else ""
        )
        lines.append(f"- **{language}** — {repository_links}{additional}")
    return "\n".join(lines)


def format_contributions(
    items: list[dict[str, Any]],
    highlighted_contributions: tuple[HighlightedContribution, ...] = (),
) -> str:
    contributions_by_repository: dict[str, list[str]] = {}
    highlighted_keys = set()

    for repository, number, title, url in highlighted_contributions:
        highlighted_keys.add((repository, number))
        contributions_by_repository.setdefault(repository, []).append(
            f"- **Featured:** [#{number} — {_escape_link_text(title)}]({url})"
        )

    for item in items:
        repository = _repository_name(str(item["repository_url"]))
        number = int(item["number"])
        if (repository, number) in highlighted_keys:
            continue
        title = _escape_link_text(str(item["title"]))
        url = _pull_request_url(item)
        contributions_by_repository.setdefault(repository, []).append(
            f"- [#{number} — {title}]({url})"
        )

    lines = []
    for repository, contributions in contributions_by_repository.items():
        if lines:
            lines.append("")
        lines.append(f"### [{repository}](https://github.com/{repository})")
        lines.append("")
        lines.extend(contributions)
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
    parser = argparse.ArgumentParser(description=__doc__)
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
    all_contributions = fetch_all_contributions(
        username=args.username,
        token=token,
    )
    contributions = select_recent_contributions(
        all_contributions,
        args.limit,
        highlighted_contributions,
    )
    skills = fetch_contribution_skills(
        all_contributions,
        username=args.username,
        token=token,
    )
    current = args.readme.read_text(encoding="utf-8")
    updated = update_readme_text(
        current,
        format_contributions(contributions, highlighted_contributions),
        format_skills(skills),
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
