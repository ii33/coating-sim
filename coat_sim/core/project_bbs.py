"""Project-owned engineering notes shared by GUI, CLI and assistant adapters.

Only trusted adapters select ``actor``. It is never accepted in a post payload.
History is append-only through this API, using the project's existing atomic
manifest publication rather than an independent database or global notebook.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal
import uuid

from .project_dependencies import ProjectError, identifier
from .project_store import ProjectStore

BBS_KIND = "project_bbs_post"
BBS_TYPES = frozenset({"journal", "progress", "research_report", "work_report", "comment"})
BBS_ACTORS = frozenset({"user", "assistant"})
MAX_TITLE_CHARS = 200
MAX_BODY_BYTES = 64 * 1024
MAX_HISTORY = 100
MAX_ATTACHMENTS = 8
BODY_FORMATS = frozenset({"plain", "markdown"})
Actor = Literal["user", "assistant"]


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ProjectError("bbs_invalid_post", field + " must be nonempty text without NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ProjectError("bbs_invalid_post", field + " must be valid UTF-8") from exc
    if ((field == "title" and (len(value) > MAX_TITLE_CHARS or "\n" in value or "\r" in value))
            or (field == "body" and size > MAX_BODY_BYTES)):
        raise ProjectError("capacity_exceeded", field + " exceeds the BBS text limit")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ProjectError("bbs_invalid_post", "Invalid BBS timestamp")
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
        if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("UTC required")
        return parsed
    except (ValueError, AttributeError) as exc:
        raise ProjectError("bbs_invalid_post", "BBS timestamps must be UTC ISO-8601") from exc



def _content_options(body_format: Any, attachments: Any) -> dict[str, Any]:
    if not isinstance(body_format, str) or body_format not in BODY_FORMATS:
        raise ProjectError("bbs_invalid_post", "body_format must be plain or markdown")
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ProjectError("capacity_exceeded", "A BBS revision permits at most eight attachments")
    if attachments:
        from .project_bbs_attachments import normalize_attachment
        normalized = [normalize_attachment(item) for item in attachments]
    else:
        normalized = []
    if len({item["asset_id"] for item in normalized}) != len(normalized):
        raise ProjectError("bbs_invalid_post", "A BBS revision cannot repeat an attachment asset")
    return {"body_format": body_format, "attachments": normalized}


def normalize_bbs_post(value: Any) -> dict[str, Any]:
    """Validate a stored envelope without generating identity or attribution."""
    fields = {"kind", "version", "post_id", "post_type", "parent_id", "author", "created_at", "history"}
    if (not isinstance(value, Mapping) or set(value) != fields
            or value.get("kind") != "coat_sim.project_bbs_post"):
        raise ProjectError("bbs_invalid_post", "Invalid BBS post fields")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ProjectError("unsupported_version", "Unsupported BBS format version")
    identifier(value["post_id"])
    if value["parent_id"] is not None:
        identifier(value["parent_id"])
        if value["parent_id"] == value["post_id"]:
            raise ProjectError("bbs_invalid_post", "A BBS post cannot reply to itself")
    if not isinstance(value["post_type"], str) or value["post_type"] not in BBS_TYPES:
        raise ProjectError("bbs_invalid_post", "Unknown BBS post type")
    if not isinstance(value["author"], str) or value["author"] not in BBS_ACTORS:
        raise ProjectError("bbs_invalid_post", "Invalid BBS author")
    previous_time = _timestamp(value["created_at"])
    history = value["history"]
    if not isinstance(history, list) or not 1 <= len(history) <= MAX_HISTORY:
        raise ProjectError("capacity_exceeded", "BBS history must have 1–100 retained revisions")
    revision_fields = {"revision", "title", "body", "archived", "edited_at", "editor"}
    for index, row in enumerate(history, 1):
        if (not isinstance(row, dict) or not revision_fields <= set(row)
                or set(row) - revision_fields - {"body_format", "attachments"}):
            raise ProjectError("bbs_invalid_post", "Invalid BBS history fields")
        if type(row["revision"]) is not int or row["revision"] != index:
            raise ProjectError("bbs_invalid_post", "BBS history revisions must be contiguous")
        _text(row["title"], "title")
        _text(row["body"], "body")
        options = _content_options(row.get("body_format", "plain"), row.get("attachments", []))
        if "attachments" in row and options["attachments"] != row["attachments"]:
            raise ProjectError("bbs_invalid_post", "BBS attachment metadata must be canonical")
        if type(row["archived"]) is not bool or not isinstance(row["editor"], str) or row["editor"] not in BBS_ACTORS:
            raise ProjectError("bbs_invalid_post", "Invalid BBS revision attribution/state")
        edited = _timestamp(row["edited_at"])
        if edited < previous_time:
            raise ProjectError("bbs_invalid_post", "BBS timestamps must not move backwards")
        previous_time = edited
    if (history[0]["edited_at"] != value["created_at"] or history[0]["editor"] != value["author"]
            or history[0]["archived"]):
        raise ProjectError("bbs_invalid_post", "BBS creation history differs from its attribution")
    return deepcopy(dict(value))


def _page(offset: Any, limit: Any, maximum: int) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise ProjectError("bbs_invalid_arguments", f"Require offset >= 0 and limit between 1 and {maximum}")


class ProjectBbsService:
    """One explicit project and trusted adapter attribution; no current project.

    A desktop caller must hold its session admission gate/owner lease. A
    headless caller uses ProjectManagementService, which acquires that lease.
    The store's operation scope serializes calls and notifications; publication
    checks the captured project fingerprint even against another store/process.
    """

    def __init__(self, store: ProjectStore, *, actor: Actor = "user", read_only: bool = False,
                 permit_comment: bool = False):
        if not isinstance(actor, str) or actor not in BBS_ACTORS:
            raise ProjectError("bbs_invalid_arguments", "Adapter actor must be user or assistant")
        if type(read_only) is not bool or type(permit_comment) is not bool:
            raise ProjectError("bbs_invalid_arguments", "read_only must be a trusted boolean")
        self.store = store
        self.actor = actor
        self.read_only = read_only
        self.permit_comment = permit_comment

    def _writable(self, *, comment_only: bool = False) -> None:
        if self.read_only and not (comment_only and self.permit_comment):
            raise ProjectError("discussion_read_only", "This BBS service is read-only")

    def _load(self):
        records, data = self.store._read_all()
        posts = {key: data[key] for key, row in records.items() if row["kind"] == BBS_KIND}
        return records, data, posts

    @staticmethod
    def _get(posts: dict, post_id: str) -> dict:
        identifier(post_id)
        if post_id not in posts:
            raise ProjectError("bbs_missing_post", "BBS post does not exist in this project")
        return posts[post_id]

    @staticmethod
    def _view(post: dict, posts: dict, *, summary: bool = False) -> dict:
        current = post["history"][-1]
        result = {key: deepcopy(post[key]) for key in ("post_id", "post_type", "parent_id", "author", "created_at")}
        result.update({key: deepcopy(current[key]) for key in ("title", "revision", "archived", "editor")})
        result.update(updated_at=current["edited_at"], history_count=len(post["history"]),
                      reply_count=sum(row["parent_id"] == post["post_id"] for row in posts.values()))
        result.update(body_format=current.get("body_format", "plain"),
                      attachments=deepcopy(current.get("attachments", [])))
        result["preview" if summary else "body"] = current["body"][:280] if summary else current["body"]
        return result

    @staticmethod
    def _expected(post: dict, expected: Any) -> None:
        if type(expected) is not int or expected < 1:
            raise ProjectError("bbs_invalid_arguments", "expected_post_revision must be a positive integer")
        if post["history"][-1]["revision"] != expected:
            raise ProjectError("stale_bbs_post", "BBS post changed; reload it before saving")

    def list_posts(self, *, post_type: str | None = None, parent_id: str | None = None,
                   include_archived: bool = False, query: str = "", offset: int = 0, limit: int = 50) -> dict:
        _page(offset, limit, 50)
        if post_type is not None and (not isinstance(post_type, str) or post_type not in BBS_TYPES):
            raise ProjectError("bbs_invalid_arguments", "Unknown BBS post type")
        if type(include_archived) is not bool or not isinstance(query, str) or len(query) > 200:
            raise ProjectError("bbs_invalid_arguments", "Invalid BBS filters")
        with self.store._operation_scope():
            _records, _data, posts = self._load()
            if parent_id is not None:
                parent = self._get(posts, parent_id)
                if parent["parent_id"] is not None:
                    raise ProjectError("bbs_invalid_arguments", "Replies belong to a top-level topic")
            matching = [post for post in posts.values() if post["parent_id"] == parent_id
                        and (post_type is None or post["post_type"] == post_type)
                        and (include_archived or not post["history"][-1]["archived"])
                        and (not query or query.casefold() in (post["history"][-1]["title"] + "\n"
                                                              + post["history"][-1]["body"]).casefold())]
            matching.sort(key=lambda post: (post["created_at"], post["post_id"]), reverse=parent_id is None)
            return {"posts": [self._view(post, posts, summary=True) for post in matching[offset:offset + limit]],
                    "total": len(matching), "offset": offset, "limit": limit}

    def read_post(self, post_id: str) -> dict:
        with self.store._operation_scope():
            _records, _data, posts = self._load()
            return self._view(self._get(posts, post_id), posts)

    def history(self, post_id: str, *, offset: int = 0, limit: int = 20) -> dict:
        _page(offset, limit, 20)
        with self.store._operation_scope():
            _records, _data, posts = self._load()
            post = self._get(posts, post_id)
            revisions = deepcopy(post["history"][offset:offset + limit])
            for row in revisions:
                row.setdefault("body_format", "plain")
                row.setdefault("attachments", [])
            return {"post_id": post_id, "revisions": revisions,
                    "total": len(post["history"]), "offset": offset, "limit": limit}

    def _create(self, post_type: str, title: str, body: str, *, parent_id: str | None = None,
                expected_post_revision: int | None = None, comment_only: bool = False,
                body_format: str = "plain", attachments: list[dict[str, Any]] | None = None) -> dict:
        self._writable(comment_only=comment_only)
        _text(title, "title")
        _text(body, "body")
        options = _content_options(body_format, [] if attachments is None else attachments)
        with self.store._operation_scope():
            records, data, posts = self._load()
            if parent_id is not None:
                parent = self._get(posts, parent_id)
                self._expected(parent, expected_post_revision)
                if parent["parent_id"] is not None:
                    raise ProjectError("bbs_invalid_arguments", "Only top-level topics accept replies")
                if parent["history"][-1]["archived"]:
                    raise ProjectError("bbs_archived", "Restore this topic before replying")
                post_type = parent["post_type"]
            identity, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
            if identity in records:
                raise ProjectError("dependency_conflict", "Generated BBS post identity collided")
            post = normalize_bbs_post({"kind": "coat_sim.project_bbs_post", "version": 1,
                "post_id": identity, "post_type": post_type, "parent_id": parent_id,
                "author": self.actor, "created_at": now, "history": [{"revision": 1, "title": title,
                    "body": body, "archived": False, "edited_at": now, "editor": self.actor, **options}]})
            records[identity] = {"id": identity, "kind": BBS_KIND, "dependencies": [], "path": "", "sha256": ""}
            data[identity] = post
            self.store._publish(records, data)
            posts[identity] = post
            return self._view(post, posts)

    def create_post(self, post_type: str, title: str, body: str, *, body_format: str = "plain",
                    attachments: list[dict[str, Any]] | None = None) -> dict:
        return self._create(post_type, title, body, body_format=body_format, attachments=attachments)

    def reply(self, post_id: str, title: str, body: str, *, expected_post_revision: int,
              body_format: str = "plain", attachments: list[dict[str, Any]] | None = None) -> dict:
        return self._create("journal", title, body, parent_id=post_id, expected_post_revision=expected_post_revision,
                            body_format=body_format, attachments=attachments)

    def comment(self, title: str, body: str, *, parent_id: str | None = None,
                expected_post_revision: int | None = None, body_format: str = "plain",
                attachments: list[dict[str, Any]] | None = None) -> dict:
        """Append a comment/reply, the sole explicitly enabled discussion write.

        This cannot select a formal report/log type, replace prior text, archive
        content or change project parameters. A reply inherits the topic type.
        """
        if parent_id is None and expected_post_revision is not None:
            raise ProjectError("bbs_invalid_arguments", "Top-level comments have no parent revision")
        return self._create("comment", title, body, parent_id=parent_id,
                            expected_post_revision=expected_post_revision, comment_only=True,
                            body_format=body_format, attachments=attachments)

    def _revise(self, post_id: str, *, expected_post_revision: int,
                title: str | None = None, body: str | None = None, archived: bool | None = None,
                body_format: str | None = None, attachments: list[dict[str, Any]] | None = None) -> dict:
        self._writable()
        with self.store._operation_scope():
            records, data, posts = self._load()
            post = self._get(posts, post_id)
            self._expected(post, expected_post_revision)
            current = post["history"][-1]
            if title is not None and current["archived"]:
                raise ProjectError("bbs_archived", "Restore this post before editing")
            revision = deepcopy(current)
            if title is not None:
                revision.update(title=_text(title, "title"), body=_text(body, "body"))
                options = _content_options(current.get("body_format", "plain") if body_format is None else body_format,
                                           current.get("attachments", []) if attachments is None else attachments)
                if body_format is not None:
                    revision["body_format"] = options["body_format"]
                if attachments is not None:
                    revision["attachments"] = options["attachments"]
            if archived is not None:
                if type(archived) is not bool:
                    raise ProjectError("bbs_invalid_arguments", "archived must be a boolean")
                revision["archived"] = archived
            if revision == current:
                return self._view(post, posts)
            if len(post["history"]) >= MAX_HISTORY:
                raise ProjectError("capacity_exceeded", "BBS history is full; prior revisions cannot be discarded")
            now = max(datetime.now(timezone.utc), _timestamp(current["edited_at"]))
            revision.update(revision=current["revision"] + 1, edited_at=now.isoformat(), editor=self.actor)
            post["history"].append(revision)
            data[post_id] = normalize_bbs_post(post)
            self.store._publish(records, data)
            return self._view(post, posts)

    def update_post(self, post_id: str, title: str, body: str, *, expected_post_revision: int,
                    body_format: str | None = None, attachments: list[dict[str, Any]] | None = None) -> dict:
        _text(title, "title")
        _text(body, "body")
        return self._revise(post_id, title=title, body=body, expected_post_revision=expected_post_revision,
                            body_format=body_format, attachments=attachments)

    def archive_post(self, post_id: str, archived: bool, *, expected_post_revision: int) -> dict:
        if type(archived) is not bool:
            raise ProjectError("bbs_invalid_arguments", "archived must be a boolean")
        return self._revise(post_id, archived=archived, expected_post_revision=expected_post_revision)
