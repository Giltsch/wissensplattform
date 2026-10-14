"""Conversation history endpoints.

Mounted under /api/conversations. Every route is `require_user` and scoped to
the current user_id — a user can never see or modify another user's
conversations (no IDOR). The chat handler in main.py reads/writes the same
tables; this module owns the CRUD surface.
"""
import logging
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from auth import require_user
from db import connection
from rate_limit import limiter
# NB: no `from __future__ import annotations` — same slowapi/Pydantic
# gotcha as main.py / totp.py / admin.py (CLAUDE.md "Things that will
# trip you up").

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])
# Message-level operations (feedback) live under /api/messages because
# putting them under /api/conversations would imply a parent path with
# nested ids, which they don't actually need — the message_id alone is
# enough to resolve ownership via the conversation FK.
messages_router = APIRouter(prefix="/api/messages", tags=["messages"])


# ---- Models --------------------------------------------------------------

class ConversationOut(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str


class FeedbackOut(BaseModel):
    rating: Literal[-1, 1]
    comment: Optional[str] = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    # Same shape as the SSE `sources` event for consistency.
    sources: Optional[List[dict]] = None
    feedback: Optional[FeedbackOut] = None
    created_at: str


class FeedbackRequest(BaseModel):
    rating: Literal[-1, 1]
    comment: Optional[str] = Field(default=None, max_length=2000)


class ConversationDetail(ConversationOut):
    messages: List[MessageOut]


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# ---- Helpers -------------------------------------------------------------

async def _ensure_owner(conv_id: int, user_id: int) -> None:
    """Raise 404 if the conversation doesn't exist *or* belongs to someone
    else. Same response either way — never reveal that an id exists for a
    different user (would leak existence)."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="conversation not found",
                )


# ---- Routes --------------------------------------------------------------

@router.get("", response_model=List[ConversationOut])
async def list_conversations(user: dict = Depends(require_user)) -> List[ConversationOut]:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, created_at, updated_at "
                "FROM conversations WHERE user_id = %s "
                "ORDER BY updated_at DESC LIMIT 200",
                (user["id"],),
            )
            rows = await cur.fetchall()
    return [
        ConversationOut(
            id=r["id"],
            title=r["title"],
            created_at=r["created_at"].isoformat(),
            updated_at=r["updated_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(user: dict = Depends(require_user)) -> ConversationOut:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO conversations(user_id) VALUES (%s) "
                "RETURNING id, title, created_at, updated_at",
                (user["id"],),
            )
            row = await cur.fetchone()
        await conn.commit()
    return ConversationOut(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


@router.get("/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: int,
    user: dict = Depends(require_user),
) -> ConversationDetail:
    """Return a conversation with messages.

    Persisted ``messages.sources`` is filtered against the user's *current*
    ACL — if a document has been restricted (or the user has been
    demoted, or the user's allowed_tags have been narrowed) since the
    answer was generated, the snippets that came from now-invisible
    documents are dropped from the reply (audit M-1, 2026-05-10).
    Filtering reads the document tag list once per visible source set;
    a single query keeps it cheap.
    """
    from acls import visibility_clause

    await _ensure_owner(conv_id, user["id"])
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, created_at, updated_at "
                "FROM conversations WHERE id = %s",
                (conv_id,),
            )
            conv = await cur.fetchone()
            # LEFT JOIN feedback so unrated messages still come through.
            await cur.execute(
                "SELECT m.id, m.role, m.content, m.sources, m.created_at, "
                "       fb.rating AS fb_rating, fb.comment AS fb_comment "
                "FROM messages m "
                "LEFT JOIN message_feedback fb ON fb.message_id = m.id "
                "WHERE m.conversation_id = %s "
                "ORDER BY m.created_at, m.id",
                (conv_id,),
            )
            msgs = await cur.fetchall()

            # Build the set of currently-visible document_ids referenced
            # across all messages, then drop sources that aren't in it.
            # This is a no-op for admins (visibility_clause -> TRUE).
            referenced_ids: set[int] = set()
            for m in msgs:
                for s in (m["sources"] or []):
                    did = s.get("document_id")
                    if isinstance(did, int):
                        referenced_ids.add(did)
            visible_ids: set[int] = set()
            if referenced_ids:
                acl_sql, acl_params = visibility_clause(
                    user.get("is_admin", False),
                    user.get("allowed_tags", []),
                    doc_alias="d",
                )
                await cur.execute(
                    f"SELECT id FROM documents AS d WHERE id = ANY(%s) AND {acl_sql}",
                    (list(referenced_ids), *acl_params),
                )
                visible_ids = {r["id"] for r in await cur.fetchall()}

    def _filter_sources(srcs):
        if not srcs:
            return srcs
        return [s for s in srcs if s.get("document_id") in visible_ids]

    return ConversationDetail(
        id=conv["id"],
        title=conv["title"],
        created_at=conv["created_at"].isoformat(),
        updated_at=conv["updated_at"].isoformat(),
        messages=[
            MessageOut(
                id=m["id"],
                role=m["role"],
                content=m["content"],
                sources=_filter_sources(m["sources"]),
                feedback=(
                    FeedbackOut(rating=m["fb_rating"], comment=m["fb_comment"])
                    if m["fb_rating"] is not None else None
                ),
                created_at=m["created_at"].isoformat(),
            )
            for m in msgs
        ],
    )


@router.patch("/{conv_id}", response_model=ConversationOut)
async def rename_conversation(
    conv_id: int,
    body: RenameRequest = Body(...),
    user: dict = Depends(require_user),
) -> ConversationOut:
    await _ensure_owner(conv_id, user["id"])
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE conversations SET title = %s, updated_at = now() "
                "WHERE id = %s "
                "RETURNING id, title, created_at, updated_at",
                (body.title.strip(), conv_id),
            )
            row = await cur.fetchone()
        await conn.commit()
    return ConversationOut(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


@router.delete("/{conv_id}", status_code=204)
async def delete_conversation(
    conv_id: int,
    user: dict = Depends(require_user),
) -> None:
    await _ensure_owner(conv_id, user["id"])
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))
        await conn.commit()


# ---- Internal helpers (used by the chat handler) ------------------------

async def fetch_history(conv_id: int) -> list[dict]:
    """Return [{role, content}] in chronological order. Caller must already
    have verified ownership — this helper trusts the conversation_id."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = %s ORDER BY created_at, id",
                (conv_id,),
            )
            rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def append_message(
    conv_id: int,
    role: str,
    content: str,
    sources: Optional[list[dict]] = None,
) -> int:
    """Insert a message and bump the conversation's updated_at. Returns the
    new message id."""
    from psycopg.types.json import Json
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO messages(conversation_id, role, content, sources) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (conv_id, role, content, Json(sources) if sources is not None else None),
            )
            row = await cur.fetchone()
            await cur.execute(
                "UPDATE conversations SET updated_at = now() WHERE id = %s",
                (conv_id,),
            )
        await conn.commit()
    return row["id"]


async def create_for_user(user_id: int, title: Optional[str] = None) -> int:
    async with connection() as conn:
        async with conn.cursor() as cur:
            if title:
                await cur.execute(
                    "INSERT INTO conversations(user_id, title) VALUES (%s, %s) RETURNING id",
                    (user_id, title),
                )
            else:
                await cur.execute(
                    "INSERT INTO conversations(user_id) VALUES (%s) RETURNING id",
                    (user_id,),
                )
            row = await cur.fetchone()
        await conn.commit()
    return row["id"]


async def maybe_set_title_from_message(conv_id: int, message: str) -> None:
    """Set the conversation title from the first user message — but only if
    it's still the default. Cheap auto-titling without an extra LLM call;
    a smarter LLM-based title can replace this later."""
    title = message.strip()
    if len(title) > 60:
        title = title[:57].rstrip() + "…"
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE conversations SET title = %s "
                "WHERE id = %s AND title = 'Neue Unterhaltung'",
                (title, conv_id),
            )
        await conn.commit()


async def _user_owns_assistant_message(message_id: int, user_id: int) -> bool:
    """Returns True iff the assistant message belongs to a conversation the
    user owns. Restricted to assistant messages — feedback on user's own
    messages doesn't make semantic sense and we don't allow it."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.id = %s AND c.user_id = %s AND m.role = 'assistant'",
                (message_id, user_id),
            )
            return await cur.fetchone() is not None


@messages_router.post("/{message_id}/feedback", status_code=204)
@limiter.limit("60/minute")
async def set_feedback(
    request: Request,
    message_id: int,
    body: FeedbackRequest = Body(...),
    user: dict = Depends(require_user),
) -> None:
    """UPSERT feedback for an assistant message you own. Same response
    whether the message doesn't exist or belongs to someone else (404 in
    both cases) — never leak that an id exists for another user."""
    if not await _user_owns_assistant_message(message_id, user["id"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO message_feedback(message_id, user_id, rating, comment) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (message_id) DO UPDATE "
                "  SET rating = EXCLUDED.rating, "
                "      comment = EXCLUDED.comment, "
                "      updated_at = now()",
                (message_id, user["id"], body.rating, body.comment),
            )
        await conn.commit()


@messages_router.delete("/{message_id}/feedback", status_code=204)
@limiter.limit("60/minute")
async def clear_feedback(
    request: Request,
    message_id: int,
    user: dict = Depends(require_user),
) -> None:
    if not await _user_owns_assistant_message(message_id, user["id"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="message not found")
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM message_feedback WHERE message_id = %s",
                (message_id,),
            )
        await conn.commit()


async def is_owner(conv_id: int, user_id: int) -> bool:
    """Plain boolean check used by the chat handler (which raises its own
    errors, so it doesn't want the helper to raise)."""
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM conversations WHERE id = %s AND user_id = %s",
                (conv_id, user_id),
            )
            return await cur.fetchone() is not None
