"""GitHub client interface and PyGithub implementation for PR review operations."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tarfile
import tempfile
from typing import Any, Protocol, cast

import httpx
from github import Auth, Github

from app.agent.schemas import PatchProposal, ReviewFinding
from app.sandbox.models import SandboxResult

logger = logging.getLogger(__name__)

BOT_MARKER_PREFIX = "<!-- pr-agent-review:"


def format_review_markdown(
    commit_sha: str,
    findings: list[ReviewFinding],
    verified: bool = False,
    proposed_patch: PatchProposal | None = None,
    sandbox_result: SandboxResult | None = None,
    status: str = "reviewed",
) -> str:
    """Format structured GitHub PR review markdown with alert badges and findings."""
    bot_marker = f"{BOT_MARKER_PREFIX} {commit_sha} -->\n"
    header = "## 🤖 Autonomous GitHub PR Code Review & Bug-Finding Agent\n\n"

    # Status alert banner
    banner = ""
    if verified and proposed_patch:
        banner = (
            "> [!TIP]\n"
            "> **Bug Confirmed & Patch Verified**: Reproduction test failed on baseline code "
            "and passed in an isolated Docker sandbox after applying the proposed fix.\n\n"
        )
    elif status == "clean":
        banner = "> [!NOTE]\n> **Clean PR**: No reproducible bugs or security flaws identified.\n\n"
    elif status == "env_fallback":
        banner = (
            "> [!IMPORTANT]\n"
            "> **Static Review Only**: Docker sandbox was unavailable or dependencies "
            "were not pre-installed in the offline sandbox image.\n\n"
        )
    elif status == "discarded":
        banner = (
            "> [!NOTE]\n"
            "> **False Positive Discarded**: Potential bug identified by static analysis, "
            "but reproduction test passed on baseline code.\n\n"
        )
    elif status == "max_retries_exceeded":
        banner = (
            "> [!WARNING]\n"
            "> **Review Only**: Proposed bug patch could not be verified in Docker sandbox "
            "within retry budget (unverified patch withheld).\n\n"
        )

    # Findings table
    findings_section = ""
    if findings:
        table_header = (
            "### 📋 Review Findings\n\n"
            "| File | Line | Severity | Category | Confidence | Summary |\n"
            "|:---|:---:|:---:|:---:|:---:|:---|\n"
        )
        table_rows: list[str] = []
        details_list: list[str] = []

        for idx, f in enumerate(findings, 1):
            sev_badge = {
                "critical": "🔴 Critical",
                "major": "🟠 Major",
                "minor": "🟡 Minor",
                "info": "🔵 Info",
            }.get(f.severity, f.severity.capitalize())

            table_rows.append(
                f"| `{f.file_path}` | {f.line} | {sev_badge} | {f.category} "
                f"| {f.confidence:.0%} | {f.title} |"
            )

            details_list.append(
                f"#### {idx}. `{f.file_path}:{f.line}` — {f.title}\n"
                f"- **Severity:** {sev_badge} | **Category:** {f.category} | "
                f"**Confidence:** {f.confidence:.0%}\n\n"
                f"{f.explanation}\n"
            )

        findings_section = (
            table_header + "\n".join(table_rows) + "\n\n" + "\n".join(details_list) + "\n\n"
        )
    else:
        findings_section = "### 📋 Review Findings\n\nNo issues found.\n\n"

    # Patch section
    patch_section = ""
    if verified and proposed_patch:
        patch_section = (
            "### 🛠️ Verified Patch Proposal\n\n"
            f"{proposed_patch.rationale}\n\n"
            f"```diff\n{proposed_patch.unified_diff}\n```\n\n"
        )

    # Sandbox diagnostics
    sb_section = ""
    if sandbox_result:
        sb_section = (
            "<details>\n<summary><b>Docker Sandbox Diagnostics</b></summary>\n\n"
            f"- **Outcome:** `{sandbox_result.outcome.value}`\n"
            f"- **Exit Code:** `{sandbox_result.exit_code}`\n"
            f"- **Execution Time:** `{sandbox_result.duration_ms:.1f}ms`\n"
            f"- **Test Passed:** `{sandbox_result.test_passed}`\n"
            "</details>\n"
        )

    return f"{bot_marker}{header}{banner}{findings_section}{patch_section}{sb_section}"


class GitHubClientProtocol(Protocol):
    """Protocol for interacting with the GitHub API."""

    async def get_pr_metadata(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch title, body, author, and base/head SHAs for a pull request."""
        ...

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Fetch raw unified diff for a pull request."""
        ...

    async def get_file_content(self, repo: str, file_path: str, ref: str) -> str | None:
        """Fetch text content of a file at a specific git ref/commit."""
        ...

    async def get_repo_archive(self, repo: str, ref: str) -> bytes:
        """Fetch a tarball archive of the repository snapshot at ref."""
        ...

    async def fetch_pr_workspace(self, repo: str, ref: str) -> tuple[str, dict[str, str]]:
        """Download and extract PR snapshot into an ephemeral directory outside OneDrive."""
        ...

    async def has_bot_reviewed(self, repo: str, pr_number: int, commit_sha: str) -> bool:
        """Check if bot has already posted a review for this commit to prevent duplicate spam."""
        ...

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        comments: list[dict[str, Any]] | None = None,
        event: str = "COMMENT",
    ) -> int:
        """Create a PR review. Returns the review ID."""
        ...

    async def post_pr_review_summary(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        findings: list[ReviewFinding],
        verified: bool = False,
        proposed_patch: PatchProposal | None = None,
        sandbox_result: SandboxResult | None = None,
        status: str = "reviewed",
    ) -> int:
        """Format and post a complete PR review, deduplicating against prior bot reviews."""
        ...


class PyGithubClient:
    """Production GitHub client using PyGithub and httpx for streaming operations."""

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._auth = Auth.Token(token)
        self._gh = Github(auth=self._auth, base_url=self.base_url)

    def _headers(self, accept: str = "application/vnd.github.v3+json") -> dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "Accept": accept,
            "User-Agent": "pr-review-agent",
        }

    async def get_pr_metadata(self, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch PR metadata including author, draft status, and commit SHAs."""

        def _fetch() -> dict[str, Any]:
            r = self._gh.get_repo(repo)
            pr = r.get_pull(pr_number)
            return {
                "title": pr.title,
                "body": pr.body or "",
                "is_draft": bool(pr.draft),
                "author": pr.user.login if pr.user else "",
                "base_sha": pr.base.sha,
                "head_sha": pr.head.sha,
            }

        return await asyncio.to_thread(_fetch)

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Fetch unified diff from GitHub pulls API with diff accept header."""
        url = f"{self.base_url}/repos/{repo}/pulls/{pr_number}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._headers("application/vnd.github.v3.diff"))
            resp.raise_for_status()
            return resp.text

    async def get_file_content(self, repo: str, file_path: str, ref: str) -> str | None:
        """Fetch file content at commit ref."""

        def _fetch() -> str | None:
            r = self._gh.get_repo(repo)
            try:
                contents = r.get_contents(file_path, ref=ref)
                if isinstance(contents, list):
                    return None
                return contents.decoded_content.decode("utf-8")
            except Exception:
                return None

        return await asyncio.to_thread(_fetch)

    async def get_repo_archive(self, repo: str, ref: str) -> bytes:
        """Fetch repository tarball at ref."""
        url = f"{self.base_url}/repos/{repo}/tarball/{ref}"
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.content

    async def fetch_pr_workspace(self, repo: str, ref: str) -> tuple[str, dict[str, str]]:
        """Download tarball and extract files into a temp directory outside OneDrive."""
        archive_bytes = await self.get_repo_archive(repo, ref)

        def _extract() -> tuple[str, dict[str, str]]:
            temp_dir = tempfile.mkdtemp(prefix="pr_repo_")
            files_dict: dict[str, str] = {}

            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
                members = tar.getmembers()
                # Find common root prefix if tarball has leading directory
                prefix = ""
                if members and "/" in members[0].name:
                    prefix = members[0].name.split("/", 1)[0] + "/"

                for m in members:
                    if not m.isfile():
                        continue
                    clean_name = m.name[len(prefix) :] if m.name.startswith(prefix) else m.name
                    clean_name = os.path.normpath(clean_name).replace("\\", "/").lstrip("/")

                    # Prevent path traversal
                    if ".." in clean_name:
                        continue

                    f = tar.extractfile(m)
                    if f is None:
                        continue

                    try:
                        content_str = f.read().decode("utf-8", errors="replace")
                        files_dict[clean_name] = content_str

                        target_path = os.path.join(temp_dir, clean_name)
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        with open(target_path, "w", encoding="utf-8") as out:
                            out.write(content_str)
                    except Exception as e:
                        logger.warning("Could not extract file %s: %s", clean_name, e)

            return temp_dir, files_dict

        return await asyncio.to_thread(_extract)

    async def has_bot_reviewed(self, repo: str, pr_number: int, commit_sha: str) -> bool:
        """Check if bot review comment already exists for this commit SHA."""

        def _check() -> bool:
            try:
                r = self._gh.get_repo(repo)
                pr = r.get_pull(pr_number)
                marker = f"{BOT_MARKER_PREFIX} {commit_sha} -->"
                return any(bool(rev.body and marker in rev.body) for rev in pr.get_reviews())
            except Exception as e:
                logger.warning("Error checking existing reviews: %s", e)
                return False

        return await asyncio.to_thread(_check)

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        comments: list[dict[str, Any]] | None = None,
        event: str = "COMMENT",
    ) -> int:
        """Publish a pull request review with inline comments."""

        def _post() -> int:
            r = self._gh.get_repo(repo)
            pr = r.get_pull(pr_number)
            commit = r.get_commit(commit_sha)
            review = pr.create_review(
                commit=commit,
                body=body,
                comments=cast(Any, comments or []),
                event=event,
            )
            return int(review.id)

        return await asyncio.to_thread(_post)

    async def post_pr_review_summary(
        self,
        repo: str,
        pr_number: int,
        commit_sha: str,
        findings: list[ReviewFinding],
        verified: bool = False,
        proposed_patch: PatchProposal | None = None,
        sandbox_result: SandboxResult | None = None,
        status: str = "reviewed",
    ) -> int:
        """Format review, deduplicate against prior posts, and publish review."""
        if await self.has_bot_reviewed(repo, pr_number, commit_sha):
            logger.info(
                "Review already posted for commit %s on %s#%d; skipping duplicate comment",
                commit_sha,
                repo,
                pr_number,
            )
            return 0

        body = format_review_markdown(
            commit_sha=commit_sha,
            findings=findings,
            verified=verified,
            proposed_patch=proposed_patch,
            sandbox_result=sandbox_result,
            status=status,
        )

        inline_comments: list[dict[str, Any]] = []

        # If verified patch has a suggestion block, attach inline review comment
        if verified and proposed_patch and proposed_patch.suggestion_block:
            target_finding = next(
                (f for f in findings if f.file_path == proposed_patch.file_path),
                findings[0] if findings else None,
            )
            line_no = target_finding.line if target_finding else 1
            inline_comments.append(
                {
                    "path": proposed_patch.file_path,
                    "line": line_no,
                    "body": (
                        f"### 🛠️ Verified Patch Suggestion\n\n"
                        f"```suggestion\n{proposed_patch.suggestion_block}\n```"
                    ),
                }
            )

        return await self.post_review(
            repo=repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
            body=body,
            comments=inline_comments if inline_comments else None,
        )


def cleanup_pr_workspace(workspace_dir: str) -> None:
    """Safely delete the temporary PR workspace directory."""
    if workspace_dir and os.path.isdir(workspace_dir):
        shutil.rmtree(workspace_dir, ignore_errors=True)
