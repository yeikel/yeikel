from __future__ import annotations

import importlib.util
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


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
    return {
        "repository": repository,
        "number": number,
        "title": title,
        "html_url": url,
        "merged_at": datetime.fromisoformat(merged_at.replace("Z", "+00:00")),
    }


def search_result(
    *,
    repository: str,
    number: int,
    title: str,
    merged_at: str | None,
) -> SimpleNamespace:
    url = f"https://github.com/{repository}/pull/{number}"
    pull_request = None
    if merged_at is not None:
        pull_request = SimpleNamespace(
            html_url=url,
            merged_at=datetime.fromisoformat(merged_at.replace("Z", "+00:00")),
        )
    return SimpleNamespace(
        repository_url=f"https://api.github.com/repos/{repository}",
        number=number,
        title=title,
        html_url=url,
        pull_request=pull_request,
    )


class FakeRepository:
    def __init__(self, github_client: "FakeGithub", name: str) -> None:
        self.github_client = github_client
        self.name = name

    @property
    def language(self) -> str | None:
        self.github_client.language_requests.append(self.name)
        return self.github_client.languages[self.name]

    def get_pull(self, number: int) -> SimpleNamespace:
        self.github_client.pull_requests.append((self.name, number))
        return SimpleNamespace(
            changed_files=self.github_client.changed_files[(self.name, number)]
        )


class FakeGithub:
    def __init__(
        self,
        *,
        search_results: list[SimpleNamespace] | None = None,
        languages: dict[str, str | None] | None = None,
        changed_files: dict[tuple[str, int], int] | None = None,
    ) -> None:
        self.search_results = search_results or []
        self.languages = languages or {}
        self.changed_files = changed_files or {}
        self.search_requests: list[dict[str, str]] = []
        self.language_requests: list[str] = []
        self.pull_requests: list[tuple[str, int]] = []

    def search_issues(self, **request: str) -> list[SimpleNamespace]:
        self.search_requests.append(request)
        return self.search_results

    def get_repo(self, repository: str) -> FakeRepository:
        return FakeRepository(self, repository)


class UpdateSelectedContributionsTest(unittest.TestCase):
    def test_creates_authenticated_pygithub_client(self) -> None:
        auth = object()
        github_client = object()
        with (
            patch.object(UPDATER.Auth, "Token", return_value=auth) as token_auth,
            patch.object(UPDATER, "Github", return_value=github_client) as client_type,
        ):
            result = UPDATER.create_github_client("test-token")

        self.assertIs(github_client, result)
        token_auth.assert_called_once_with("test-token")
        client_type.assert_called_once_with(
            auth=auth,
            per_page=UPDATER.PER_PAGE,
            user_agent="yeikel-profile-readme-updater",
        )

    def test_loads_highlighted_contributions_from_json(self) -> None:
        self.assertIn(
            (
                "dependabot/dependabot-core",
                14114,
                "Add support for calendar-based versions for Maven and Gradle",
                "https://github.com/dependabot/dependabot-core/pull/14114",
                "Maven and Gradle version compatibility",
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
                            "impact": "Maven Wrapper support",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "positive integer number"):
                UPDATER.load_highlighted_contributions(path)

    def test_fetches_and_sorts_contributions_by_merge_time(self) -> None:
        items = [
            search_result(
                repository="example/older",
                number=1,
                title="Older",
                merged_at="2026-01-01T00:00:00Z",
            ),
            search_result(
                repository="example/newest",
                number=3,
                title="Newest",
                merged_at="2026-03-01T00:00:00Z",
            ),
            search_result(
                repository="example/middle",
                number=2,
                title="Middle",
                merged_at="2026-02-01T00:00:00Z",
            ),
        ]
        github_client = FakeGithub(search_results=items)

        result = UPDATER.fetch_contributions(
            username="yeikel",
            limit=2,
            github_client=github_client,
        )

        self.assertEqual([3, 2], [item["number"] for item in result])
        self.assertEqual(
            [
                {
                    "query": "is:pr author:yeikel is:merged is:public -user:yeikel",
                    "sort": "updated",
                    "order": "desc",
                }
            ],
            github_client.search_requests,
        )

    def test_rejects_search_results_without_merged_contributions(self) -> None:
        github_client = FakeGithub(
            search_results=[
                search_result(
                    repository="example/unmerged",
                    number=1,
                    title="Unmerged",
                    merged_at=None,
                )
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "no merged public contributions"):
            UPDATER.fetch_contributions(
                username="yeikel",
                limit=1,
                github_client=github_client,
            )

    def test_deduplicates_pygithub_search_results(self) -> None:
        duplicate = search_result(
            repository="example/project",
            number=42,
            title="Contribution",
            merged_at="2026-01-01T00:00:00Z",
        )
        github_client = FakeGithub(search_results=[duplicate, duplicate])

        result = UPDATER.fetch_all_contributions(
            username="yeikel",
            github_client=github_client,
        )

        self.assertEqual(1, len(result))
        self.assertEqual(42, result[0]["number"])

    def test_accepts_merged_highlighted_contributions(self) -> None:
        merged = contribution(
            repository="example/project",
            number=42,
            title="Improve reliability",
            merged_at="2026-01-01T00:00:00Z",
        )

        UPDATER.validate_highlighted_contributions(
            [merged],
            (
                (
                    "example/project",
                    42,
                    "Improve reliability",
                    "https://github.com/example/project/pull/42",
                    "Reliability",
                ),
            ),
        )

    def test_rejects_unmerged_highlighted_contributions(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            r"merged public pull requests.*example/project#42",
        ):
            UPDATER.validate_highlighted_contributions(
                [],
                (
                    (
                        "example/project",
                        42,
                        "Work in progress",
                        "https://github.com/example/project/pull/42",
                        "Reliability",
                    ),
                ),
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
            "#### Recent merged work\n\n"
            "- [#123: Handle \\[quoted\\] \\\\ values]"
            "(https://github.com/dependabot/dependabot-core/pull/123)\n"
            "- [#122: Improve updater]"
            "(https://github.com/dependabot/dependabot-core/pull/122)\n\n"
            "### [github/advisory-database]"
            "(https://github.com/github/advisory-database)\n\n"
            "#### Recent merged work\n\n"
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
            for repository, number, title, _url, _impact in reversed(
                HIGHLIGHTED_CONTRIBUTIONS
            )
        ]

        formatted = UPDATER.format_contributions(
            highlighted_items,
            HIGHLIGHTED_CONTRIBUTIONS,
        )

        self.assertEqual(1, formatted.count("#### Selected impact"))
        self.assertNotIn("Featured", formatted)
        self.assertEqual(
            1,
            formatted.count(
                "### [dependabot/dependabot-core]"
                "(https://github.com/dependabot/dependabot-core)"
            ),
        )
        previous_position = -1
        for _repository, number, title, url, _impact in HIGHLIGHTED_CONTRIBUTIONS:
            expected = f"[#{number}: {title}]({url})"
            self.assertIn(expected, formatted)
            self.assertEqual(1, formatted.count(url))
            position = formatted.index(expected)
            self.assertGreater(position, previous_position)
            previous_position = position
        self.assertEqual(1, formatted.count("**Security-update correctness:**"))

    def test_highlighted_contribution_does_not_consume_recent_slot(self) -> None:
        items = [
            contribution(
                repository="dependabot/dependabot-core",
                number=14114,
                title="Add support for calendar-based versions for Maven and Gradle",
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
        github_client = FakeGithub(
            languages={
                "example/java-one": "Java",
                "example/ruby": "Ruby",
                "example/java-two": "Java",
            }
        )

        skills = UPDATER.fetch_contribution_skills(
            items,
            github_client=github_client,
            highlighted_contributions=(
                (
                    "example/ruby",
                    99,
                    "Curated Ruby contribution",
                    "https://github.com/example/ruby/pull/99",
                    "Ruby ecosystem support",
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
        self.assertEqual(
            ["example/java-one", "example/ruby", "example/java-two"],
            github_client.language_requests,
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
        github_client = FakeGithub(
            languages={
                "example/substantial-upgrade": "Java",
                "example/kotlin": "Kotlin",
                "example/mdx": "MDX",
                "example/go": "Go",
            },
            changed_files={
                ("example/dependency-only", 10): 2,
                ("example/substantial-upgrade", 11): 3,
            },
        )

        skills = UPDATER.fetch_contribution_skills(
            items,
            github_client=github_client,
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
        self.assertEqual(
            [
                ("example/dependency-only", 10),
                ("example/substantial-upgrade", 11),
            ],
            github_client.pull_requests,
        )
        self.assertEqual(
            [
                "example/substantial-upgrade",
                "example/kotlin",
                "example/mdx",
                "example/go",
            ],
            github_client.language_requests,
        )

    def test_formats_ranked_skills_with_repository_evidence(self) -> None:
        formatted = UPDATER.format_skills(
            {
                "Ruby": [
                    (
                        "dependabot/dependabot-core",
                        "https://github.com/dependabot/dependabot-core/pull/14812",
                    ),
                    ("excon/excon", "https://github.com/excon/excon/pull/900"),
                    (
                        "deitch/docker_registry2",
                        "https://github.com/deitch/docker_registry2/pull/85",
                    ),
                    ("example/ruby", "https://github.com/example/ruby/pull/1"),
                ],
                "Java": [
                    ("example/one", "https://github.com/example/one/pull/1"),
                    ("example/two", "https://github.com/example/two/pull/2"),
                    ("example/three", "https://github.com/example/three/pull/3"),
                    ("example/four", "https://github.com/example/four/pull/4"),
                    ("example/five", "https://github.com/example/five/pull/5"),
                ],
            },
            highlighted_contributions=HIGHLIGHTED_CONTRIBUTIONS,
        )

        self.assertEqual(
            "Repository primary languages represented in recent merged contributions, "
            "excluding minor dependency and typo fixes. Each repository links to a "
            "representative contribution.\n\n"
            "| Repository language | Contribution evidence |\n"
            "| --- | --- |\n"
            "| **Java** | [example/one](https://github.com/example/one/pull/1), "
            "[example/two](https://github.com/example/two/pull/2) "
            "(+3 more repositories) |\n"
            "| **Ruby** | Highlighted examples: "
            "[dependabot/dependabot-core#14114]"
            "(https://github.com/dependabot/dependabot-core/pull/14114), "
            "[dependabot/dependabot-core#15191]"
            "(https://github.com/dependabot/dependabot-core/pull/15191) "
            "(+3 more repositories) |",
            formatted,
        )

    def test_limits_default_skills_to_four_languages(self) -> None:
        formatted = UPDATER.format_skills(
            {
                "Java": [("example/java", "https://github.com/example/java/pull/1")],
                "Ruby": [("example/ruby", "https://github.com/example/ruby/pull/1")],
                "Go": [("example/go", "https://github.com/example/go/pull/1")],
                "Scala": [
                    ("example/scala", "https://github.com/example/scala/pull/1")
                ],
                "Shell": [
                    ("example/shell", "https://github.com/example/shell/pull/1")
                ],
            }
        )

        self.assertEqual(4, formatted.count("| **"))
        self.assertNotIn("**Shell**", formatted)

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
