"""GitHub client interface and PyGithub implementation.

Provides an async protocol for:
- Fetching unified PR diff
- Fetching file content at ref
- Fetching repository archive tarball
- Posting pull request reviews and suggestion comments
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast

import httpx
from github import Auth, Github


class GitHubClientProtocol(Protocol):
    """Protocol for interacting with the GitHub API."""

    async def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Fetch raw unified diff for a pull request."""
        ...

    async def get_file_content(self, repo: str, file_path: str, ref: str) -> str | None:
        """Fetch text content of a file at a specific git ref/commit."""
        ...

    async def get_repo_archive(self, repo: str, ref: str) -> bytes:
        """Fetch a tarball archive of the repository snapshot at ref."""
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


class PyGithubClient:
    """Production GitHub client using PyGithub and httpx for streaming large payloads."""

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
