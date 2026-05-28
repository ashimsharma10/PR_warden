from __future__ import annotations

from pydantic import BaseModel


class GitHubUser(BaseModel):
    login: str
    id: int


class Repository(BaseModel):
    id: int
    full_name: str
    owner: GitHubUser


class Installation(BaseModel):
    id: int


class Ref(BaseModel):
    ref: str
    sha: str


class PullRequest(BaseModel):
    number: int
    title: str
    body: str | None = None
    state: str
    draft: bool = False
    head: Ref
    base: Ref
    user: GitHubUser
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0
    requested_reviewers: list[GitHubUser] = []


class PullRequestEvent(BaseModel):
    action: str
    number: int
    pull_request: PullRequest
    repository: Repository
    sender: GitHubUser
    installation: Installation


class Comment(BaseModel):
    id: int
    body: str
    user: GitHubUser


class Issue(BaseModel):
    number: int
    pull_request: dict | None = None  # present when comment is on a PR


class IssueCommentEvent(BaseModel):
    action: str
    comment: Comment
    issue: Issue
    repository: Repository
    sender: GitHubUser
    installation: Installation
