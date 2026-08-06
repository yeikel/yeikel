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

    def test_rejects_incomplete_search_results(self) -> None:
        def fake_open(_request, timeout):
            self.assertEqual(30, timeout)
            return FakeResponse(
                {
                    "total_count": 1,
                    "incomplete_results": True,
                    "items": [
                        contribution(
                            repository="example/incomplete",
                            number=1,
                            title="Incomplete",
                            merged_at="2026-01-01T00:00:00Z",
                        )
                    ],
                }
            )

        with self.assertRaisesRegex(RuntimeError, "incomplete search results"):
            UPDATER.fetch_contributions(
                username="yeikel",
                limit=1,
                open_url=fake_open,
            )

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

    def test_highlights_pinned_contributions_without_duplicates(self) -> None:
        highlighted_items = [
            contribution(
                repository="dependabot/dependabot-core",
                number=15226,
                title="Gate YARN_NPM_MINIMAL_AGE_GATE on Yarn 4.10+",
                merged_at="2026-04-01T00:00:00Z",
            ),
            contribution(
                repository="dependabot/dependabot-core",
                number=15191,
                title="Disable `npmMinimalAgeGate` for Yarn Berry security updates",
                merged_at="2026-03-15T00:00:00Z",
            ),
            contribution(
                repository="dependabot/dependabot-core",
                number=14812,
                title="Add support for the Maven Wrapper",
                merged_at="2026-03-01T00:00:00Z",
            ),
        ]

        formatted = UPDATER.format_contributions(
            highlighted_items,
            UPDATER.HIGHLIGHTED_CONTRIBUTIONS,
        )

        self.assertEqual(
            "### [dependabot/dependabot-core]"
            "(https://github.com/dependabot/dependabot-core)\n\n"
            "- **Featured:** [#14812 — Add support for the Maven Wrapper]"
            "(https://github.com/dependabot/dependabot-core/pull/14812)\n"
            "- **Featured:** "
            "[#15226 — Gate YARN_NPM_MINIMAL_AGE_GATE on Yarn 4.10+]"
            "(https://github.com/dependabot/dependabot-core/pull/15226)\n"
            "- **Featured:** "
            "[#15191 — Disable `npmMinimalAgeGate` for Yarn Berry security updates]"
            "(https://github.com/dependabot/dependabot-core/pull/15191)",
            formatted,
        )
        self.assertEqual(1, formatted.count("/pull/14812"))
        self.assertEqual(1, formatted.count("/pull/15226"))
        self.assertEqual(1, formatted.count("/pull/15191"))

    def test_highlighted_contribution_does_not_consume_recent_slot(self) -> None:
        items = [
            contribution(
                repository="dependabot/dependabot-core",
                number=14812,
                title="Add support for the Maven Wrapper",
                merged_at="2026-04-01T00:00:00Z",
            ),
            contribution(
                repository="example/one",
                number=3,
                title="First recent contribution",
                merged_at="2026-03-01T00:00:00Z",
            ),
            contribution(
                repository="example/two",
                number=2,
                title="Second recent contribution",
                merged_at="2026-02-01T00:00:00Z",
            ),
            contribution(
                repository="example/three",
                number=1,
                title="Third recent contribution",
                merged_at="2026-01-01T00:00:00Z",
            ),
        ]

        selected = UPDATER.select_recent_contributions(
            items,
            limit=3,
            highlighted_contributions=UPDATER.HIGHLIGHTED_CONTRIBUTIONS,
        )

        self.assertEqual([3, 2, 1], [item["number"] for item in selected])

    def test_derives_skills_from_unique_repository_languages(self) -> None:
        items = [
            contribution(
                repository="example/java-one",
                number=3,
                title="Newest",
                merged_at="2026-03-01T00:00:00Z",
            ),
            contribution(
                repository="example/ruby",
                number=2,
                title="Middle",
                merged_at="2026-02-01T00:00:00Z",
            ),
            contribution(
                repository="example/java-one",
                number=1,
                title="Duplicate repository",
                merged_at="2026-01-01T00:00:00Z",
            ),
            contribution(
                repository="example/java-two",
                number=4,
                title="Older Java repository",
                merged_at="2025-12-01T00:00:00Z",
            ),
        ]
        languages = {
            "https://api.github.com/repos/example/java-one": "Java",
            "https://api.github.com/repos/example/ruby": "Ruby",
            "https://api.github.com/repos/example/java-two": "Java",
        }
        requests = []

        def fake_open(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"language": languages[request.full_url]})

        skills = UPDATER.fetch_contribution_skills(
            items,
            username="yeikel",
            token="test-token",
            open_url=fake_open,
        )

        self.assertEqual(
            {
                "Java": ["example/java-one", "example/java-two"],
                "Ruby": ["example/ruby"],
            },
            skills,
        )
        self.assertEqual(3, len(requests))
        self.assertTrue(
            all(
                request.get_header("Authorization") == "Bearer test-token"
                and timeout == 30
                for request, timeout in requests
            )
        )

    def test_formats_ranked_skills_with_repository_evidence(self) -> None:
        formatted = UPDATER.format_skills(
            {
                "Ruby": ["dependabot/dependabot-core"],
                "Java": [
                    "example/one",
                    "example/two",
                    "example/three",
                    "example/four",
                ],
            }
        )

        self.assertEqual(
            "Primary languages across public repositories in my recent merged "
            "contribution history:\n\n"
            "- **Java** — [example/one](https://github.com/example/one), "
            "[example/two](https://github.com/example/two), "
            "[example/three](https://github.com/example/three) "
            "(+1 more repository)\n"
            "- **Ruby** — [dependabot/dependabot-core]"
            "(https://github.com/dependabot/dependabot-core)",
            formatted,
        )

    def test_prefers_more_recent_language_when_repository_counts_tie(self) -> None:
        formatted = UPDATER.format_skills(
            {
                "Rust": ["example/recent"],
                "CSS": ["example/older"],
            },
            max_skills=1,
        )

        self.assertIn("**Rust**", formatted)
        self.assertNotIn("**CSS**", formatted)

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

    def test_updates_contribution_and_skill_sections(self) -> None:
        original = (
            f"{UPDATER.SKILLS_START_MARKER}\n"
            "- old skill\n"
            f"{UPDATER.SKILLS_END_MARKER}\n"
            f"{UPDATER.START_MARKER}\n"
            "- old contribution\n"
            f"{UPDATER.END_MARKER}\n"
        )

        updated = UPDATER.update_readme_text(original, "- contribution", "- skill")

        self.assertEqual(
            f"{UPDATER.SKILLS_START_MARKER}\n"
            f"{UPDATER.SKILLS_GENERATED_NOTICE}\n"
            "- skill\n"
            f"{UPDATER.SKILLS_END_MARKER}\n"
            f"{UPDATER.START_MARKER}\n"
            f"{UPDATER.GENERATED_NOTICE}\n"
            "- contribution\n"
            f"{UPDATER.END_MARKER}\n",
            updated,
        )

    def test_rejects_missing_markers(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "marker pair"):
            UPDATER.update_readme_text("No generated section", "- new")


if __name__ == "__main__":
    unittest.main()
