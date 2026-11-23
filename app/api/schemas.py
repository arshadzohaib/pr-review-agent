"""Pydantic v2 models for GitHub webhook events.

Models follow the GitHub REST & Webhooks specification for 'pull_request' events.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GitHubUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    login: str
    id: int | None = None
    type: str = "User"


class GitHubRepo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str
    name: str = ""
    private: bool = False
    html_url: str = ""
    owner: GitHubUser | None = None


class GitHubPRRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sha: str
    ref: str = ""
    repo: GitHubRepo | None = None


class GitHubLabel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str


class GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    title: str = ""
    state: str = "open"
    draft: bool = False
    user: GitHubUser
    head: GitHubPRRef
    base: GitHubPRRef
    labels: list[GitHubLabel] = Field(default_factory=list)
    html_url: str = ""


class PullRequestWebhookEvent(BaseModel):
    """Event payload for GitHub 'pull_request' webhook."""

    model_config = ConfigDict(extra="ignore")

    action: str
    number: int
    pull_request: GitHubPullRequest
    repository: GitHubRepo
    sender: GitHubUser | None = None

    @property
    def repo_full_name(self) -> str:
        return self.repository.full_name

    @property
    def head_sha(self) -> str:
        return self.pull_request.head.sha

    @property
    def base_sha(self) -> str:
        return self.pull_request.base.sha

    @property
    def label_names(self) -> frozenset[str]:
        return frozenset(label.name for label in self.pull_request.labels)


class WebhookResponse(BaseModel):
    """Standardized response from the webhook endpoint."""

    status: str
    message: str
    delivery_id: str = ""
    job_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
