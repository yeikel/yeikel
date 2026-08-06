from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
import unittest


SCRIPT_PATH = (
    Path(__file__).parents[1] / "update_selected_contributions.py"
)
SPEC = importlib.util.spec_from_file_location("update_selected_contributions", SCRIPT_PATH)
assert SPEC and SPEC.loader
UPDATER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATER)
HIGHLIGHTED_CONTRIBUTIONS = UPDATER.load_highlighted_contributions(
    UPDATER.DEFAULT_HIGHLIGHTS_PATH
)


def contribution(
    *,
    repository: str,
    number: int,
    title: str,
    merged_at: str,
) -> dict[str, object]:
    url = f"https://github.com/{repository}/pull/{number}"
    api_url = f"https://api.github.com/repos/{repository}/pulls/{number}"
    return {
        "number": number,
        "title": title,
        "repository_url": f"https://api.github.com/repos/{repository}",
        "html_url": url,
        "pull_request": {
            "html_url": url,
            "merged_at": merged_at,
            "url": api_url,
        },
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
    def test_loads_highlighted_contributions_from_json(self) -> None:
        self.assertIn(
            (
                "dependabot/dependabot-core",
                14812,
                "Add support for the Maven Wrapper",
                "https://github.com/dependabot/dependabot-core/pull/14812",
            ),
            HIGHLIGHTED_CONTRIBUTIONS,
        )

    def test_rejects_invalid_highlighted_contributions_json(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "highlights.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "repository": "dependabot/dependabot-core",
                            "number": "14812",
                            "title": "Add support for the Maven Wrapper",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "positive integer number"):
                UPDATER.load_highlighted_contributions(path)

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
            "#### Recent contributions\n\n"
            "- [#123: Handle \\[quoted\\] \\\\ values]"
            "(https://github.com/dependabot/dependabot-core/pull/123)\n"
            "- [#122: Improve updater]"
            "(https://github.com/dependabot/dependabot-core/pull/122)\n\n"
            "### [github/advisory-database]"
            "(https://github.com/github/advisory-database)\n\n"
            "#### Recent contributions\n\n"
            "- [#456: Update advisory]"
            "(https://github.com/github/advisory-database/pull/456)",
            formatted,
        )

    def test_highlights_pinned_contributions_without_duplicates(self) -> None:
        highlighted_items = [
            contribution(
                repository=repository,
                number=number,
                title=title,
                merged_at="2026-01-01T00:00:00Z",
            )
            for repository, number, title, _url in reversed(
                HIGHLIGHTED_CONTRIBUTIONS
            )
        ]

        formatted = UPDATER.format_contributions(
            highlighted_items,
            HIGHLIGHTED_CONTRIBUTIONS,
        )

        self.assertEqual(1, formatted.count("#### Highlights"))
        self.assertNotIn("Featured", formatted)
        self.assertEqual(
            1,
            formatted.count(
                "### [dependabot/dependabot-core]"
                "(https://github.com/dependabot/dependabot-core)"
            ),
        )
        previous_position = -1
        for _repository, number, title, url in HIGHLIGHTED_CONTRIBUTIONS:
            expected = f"- [#{number}: {title}]({url})"
            self.assertIn(expected, formatted)
            self.assertEqual(1, formatted.count(url))
            position = formatted.index(expected)
            self.assertGreater(position, previous_position)
            previous_position = position

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
            highlighted_contributions=HIGHLIGHTED_CONTRIBUTIONS,
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
            highlighted_contributions=(
                (
                    "example/ruby",
                    99,
                    "Curated Ruby contribution",
                    "https://github.com/example/ruby/pull/99",
                ),
            ),
        )

        self.assertEqual(
            {
                "Java": [
                    (
                        "example/java-one",
                        "https://github.com/example/java-one/pull/3",
                    ),
                    (
                        "example/java-two",
                        "https://github.com/example/java-two/pull/4",
                    ),
                ],
                "Ruby": [
                    ("example/ruby", "https://github.com/example/ruby/pull/99")
                ],
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

    def test_excludes_low_signal_skill_contributions_and_languages(self) -> None:
        items = [
            contribution(
                repository="example/dependency-only",
                number=10,
                title="Bump widget from 1.0.0 to 1.0.1",
                merged_at="2026-06-01T00:00:00Z",
            ),
            contribution(
                repository="example/substantial-upgrade",
                number=11,
                title="Upgrade framework to 2.0.0",
                merged_at="2026-05-01T00:00:00Z",
            ),
            contribution(
                repository="example/typo-only",
                number=12,
                title="Fix typo in README",
                merged_at="2026-04-01T00:00:00Z",
            ),
            contribution(
                repository="opencontainers/distribution-spec",
                number=465,
                title="Improve specification",
                merged_at="2026-03-01T00:00:00Z",
            ),
            contribution(
                repository="example/kotlin",
                number=13,
                title="Add new capability",
                merged_at="2026-02-01T00:00:00Z",
            ),
            contribution(
                repository="example/mdx",
                number=14,
                title="Document new capability",
                merged_at="2026-01-01T00:00:00Z",
            ),
            contribution(
                repository="example/go",
                number=15,
                title="Add JSON output",
                merged_at="2025-12-01T00:00:00Z",
            ),
        ]
        responses = {
            "https://api.github.com/repos/example/dependency-only/pulls/10": {
                "changed_files": 2
            },
            "https://api.github.com/repos/example/substantial-upgrade/pulls/11": {
                "changed_files": 3
            },
            "https://api.github.com/repos/example/substantial-upgrade": {
                "language": "Java"
            },
            "https://api.github.com/repos/example/kotlin": {"language": "Kotlin"},
            "https://api.github.com/repos/example/mdx": {"language": "MDX"},
            "https://api.github.com/repos/example/go": {"language": "Go"},
        }
        requests = []

        def fake_open(request, timeout):
            requests.append((request, timeout))
            return FakeResponse(responses[request.full_url])

        skills = UPDATER.fetch_contribution_skills(
            items,
            username="yeikel",
            token="test-token",
            open_url=fake_open,
        )

        self.assertEqual(
            {
                "Java": [
                    (
                        "example/substantial-upgrade",
                        "https://github.com/example/substantial-upgrade/pull/11",
                    )
                ],
                "Go": [
                    ("example/go", "https://github.com/example/go/pull/15")
                ],
            },
            skills,
        )
        self.assertEqual(6, len(requests))

    def test_formats_ranked_skills_with_repository_evidence(self) -> None:
        formatted = UPDATER.format_skills(
            {
                "Ruby": [
                    (
                        "dependabot/dependabot-core",
                        "https://github.com/dependabot/dependabot-core/pull/14812",
                    )
                ],
                "Java": [
                    ("example/one", "https://github.com/example/one/pull/1"),
                    ("example/two", "https://github.com/example/two/pull/2"),
                    ("example/three", "https://github.com/example/three/pull/3"),
                    ("example/four", "https://github.com/example/four/pull/4"),
                ],
            }
        )

        self.assertEqual(
            "Primary languages across public repositories in my recent merged contribution "
            "history. Each repository links to a meaningful contribution: a curated highlight "
            "when available, otherwise my most recently merged contribution. Small dependency "
            "updates and typo corrections are excluded:\n\n"
            "| Language | Contribution evidence |\n"
            "| --- | --- |\n"
            "| **Java** | [example/one](https://github.com/example/one/pull/1), "
            "[example/two](https://github.com/example/two/pull/2), "
            "[example/three](https://github.com/example/three/pull/3) "
            "(+1 more repository) |\n"
            "| **Ruby** | [dependabot/dependabot-core]"
            "(https://github.com/dependabot/dependabot-core/pull/14812) |",
            formatted,
        )

    def test_prefers_more_recent_language_when_repository_counts_tie(self) -> None:
        formatted = UPDATER.format_skills(
            {
                "Rust": [
                    ("example/recent", "https://github.com/example/recent/pull/2")
                ],
                "CSS": [
                    ("example/older", "https://github.com/example/older/pull/1")
                ],
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
