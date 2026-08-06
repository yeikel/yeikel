#!/usr/bin/env python3
"""Update the generated contribution list in the profile README."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable
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
PER_PAGE = 100
MAX_SEARCH_RESULTS = 1_000


def _request_page(
    *,
    username: str,
    page: int,
    token: str | None,
    open_url: Callable[..., Any],
) -> dict[str, Any]:
    query = f"is:pr author:{username} is:merged is:public -user:{username}"
    url = f"{API_URL}?{urlencode({'q': query, 'sort': 'updated', 'order': 'desc', 'per_page': PER_PAGE, 'page': page})}"
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

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("GitHub returned an unexpected search response")
    return payload


def fetch_contributions(
    username: str,
    limit: int,
    token: str | None = None,
    open_url: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the most recently merged public PRs outside the user's repos."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")

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
    return merged_items[:limit]


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


def format_contributions(items: list[dict[str, Any]]) -> str:
    lines = []
    for item in items:
        repository = _repository_name(str(item["repository_url"]))
        number = int(item["number"])
        title = _escape_link_text(str(item["title"]))
        url = _pull_request_url(item)
        lines.append(f"- [{repository}#{number} — {title}]({url})")
    return "\n".join(lines)


def update_readme_text(readme: str, contribution_list: str) -> str:
    if readme.count(START_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise RuntimeError("README must contain exactly one contribution marker pair")

    before, marker, remainder = readme.partition(START_MARKER)
    _, end_marker, after = remainder.partition(END_MARKER)
    if not marker or not end_marker:
        raise RuntimeError("README contribution markers are missing or out of order")

    generated = (
        f"{START_MARKER}\n"
        f"{GENERATED_NOTICE}\n"
        f"{contribution_list}\n"
        f"{END_MARKER}"
    )
    return f"{before}{generated}{after}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readme", nargs="?", type=Path, default=Path("README.md"))
    parser.add_argument(
        "--username",
        default=os.environ.get("PROFILE_USERNAME") or os.environ.get("GITHUB_REPOSITORY_OWNER"),
        help="GitHub username whose contributions should be selected",
    )
    parser.add_argument("--limit", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.username:
        raise SystemExit("--username or PROFILE_USERNAME is required")

    contributions = fetch_contributions(
        username=args.username,
        limit=args.limit,
        token=os.environ.get("GITHUB_TOKEN"),
    )
    current = args.readme.read_text(encoding="utf-8")
    updated = update_readme_text(current, format_contributions(contributions))

    if updated == current:
        print("Selected contributions are already current")
        return 0

    with args.readme.open("w", encoding="utf-8", newline="\n") as readme_file:
        readme_file.write(updated)
    print(f"Updated {args.readme}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
