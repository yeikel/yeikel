from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import unittest


SCRIPT_PATH = (
    Path(__file__).parents[1]
    / ".github"
    / "scripts"
    / "update_selected_contributions.py"
)
SPEC = importlib.util.spec_from_file_location("update_selected_contributions", SCRIPT_PATH)
assert SPEC and SPEC.loader
UPDATER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATER)


def contribution(
    *,
    repository: str,
    number: int,
    title: str,
    merged_at: str,
) -> dict[str, object]:
    url = f"https://github.com/{repository}/pull/{number}"
    return {
        "number": number,
        "title": title,
        "repository_url": f"https://api.github.com/repos/{repository}",
        "html_url": url,
        "pull_request": {"html_url": url, "merged_at": merged_at},
    }


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class UpdateSelectedContributionsTest(unittest.TestCase):
    def test_fetches_and_sorts_contributions_by_merge_time(self) -> None:
        requests = []
        items = [
            contribution(
                repository="example/older",
                number=1,
                title="Older",
                merged_at="2026-01-01T00:00:00Z",
            ),
            contribution(
                repository="example/newest",
                number=3,
                title="Newest",
                merged_at="2026-03-01T00:00:00Z",
            ),
            contribution(
                repository="example/middle",
                number=2,
                title="Middle",
                merged_at="2026-02-01T00:00:00Z",
            ),
        ]

        def fake_open(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"total_count": len(items), "items": items})

        result = UPDATER.fetch_contributions(
            username="yeikel",
            limit=2,
            token="test-token",
            open_url=fake_open,
        )

        self.assertEqual([3, 2], [item["number"] for item in result])
        self.assertEqual(1, len(requests))
        request, timeout = requests[0]
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(
            ["is:pr author:yeikel is:merged is:public -user:yeikel"],
            query["q"],
        )
        self.assertEqual("Bearer test-token", request.get_header("Authorization"))
        self.assertEqual(30, timeout)

    def test_groups_contributions_by_repository_and_escapes_title(self) -> None:
        formatted = UPDATER.format_contributions(
            [
                contribution(
                    repository="dependabot/dependabot-core",
                    number=123,
                    title="Handle [quoted] \\ values",
                    merged_at="2026-03-01T00:00:00Z",
                ),
                contribution(
                    repository="github/advisory-database",
                    number=456,
                    title="Update advisory",
                    merged_at="2026-02-01T00:00:00Z",
                ),
                contribution(
                    repository="dependabot/dependabot-core",
                    number=122,
                    title="Improve updater",
                    merged_at="2026-01-01T00:00:00Z",
                ),
            ]
        )

        self.assertEqual(
            "### [dependabot/dependabot-core]"
            "(https://github.com/dependabot/dependabot-core)\n\n"
            "- [#123 — Handle \\[quoted\\] \\\\ values]"
            "(https://github.com/dependabot/dependabot-core/pull/123)\n"
            "- [#122 — Improve updater]"
            "(https://github.com/dependabot/dependabot-core/pull/122)\n\n"
            "### [github/advisory-database]"
            "(https://github.com/github/advisory-database)\n\n"
            "- [#456 — Update advisory]"
            "(https://github.com/github/advisory-database/pull/456)",
            formatted,
        )

    def test_highlights_pinned_contribution_without_duplicate(self) -> None:
        highlighted_item = contribution(
            repository="dependabot/dependabot-core",
            number=14812,
            title="Add support for the Maven Wrapper",
            merged_at="2026-03-01T00:00:00Z",
        )

        formatted = UPDATER.format_contributions(
            [highlighted_item],
            UPDATER.HIGHLIGHTED_CONTRIBUTIONS,
        )

        self.assertEqual(
            "### [dependabot/dependabot-core]"
            "(https://github.com/dependabot/dependabot-core)\n\n"
            "- **Featured:** [#14812 — Add support for the Maven Wrapper]"
            "(https://github.com/dependabot/dependabot-core/pull/14812)",
            formatted,
        )
        self.assertEqual(1, formatted.count("/pull/14812"))

    def test_replaces_only_the_generated_section(self) -> None:
        original = (
            "Before\n"
            f"{UPDATER.START_MARKER}\n"
            "- old\n"
            f"{UPDATER.END_MARKER}\n"
            "After\n"
        )

        updated = UPDATER.update_readme_text(original, "- new")

        self.assertEqual(
            "Before\n"
            f"{UPDATER.START_MARKER}\n"
            f"{UPDATER.GENERATED_NOTICE}\n"
            "- new\n"
            f"{UPDATER.END_MARKER}\n"
            "After\n",
            updated,
        )
        self.assertEqual(updated, UPDATER.update_readme_text(updated, "- new"))

    def test_rejects_missing_markers(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "marker pair"):
            UPDATER.update_readme_text("No generated section", "- new")


if __name__ == "__main__":
    unittest.main()
