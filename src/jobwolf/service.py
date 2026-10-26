"""Deterministic, provider-independent Person Map mutations and review rules."""

import hashlib
import html
import re
from typing import Protocol

from pydantic import ValidationError

from .documents import checked_text
from .models import (
    DOMAINS,
    AcceptRequest,
    Brief,
    ContextBundle,
    ContextDocument,
    ContextSelection,
    DocumentSaveRequest,
    DocumentSelection,
    DocumentSource,
    Message,
    ModelReply,
    Opportunity,
    Proposal,
    Statement,
    StatementDraft,
    Workspace,
    now,
)
from .storage import ConflictError, StorageError


class UserError(Exception):
    """An actionable error whose message contains no private input values."""


class MissingRecord(UserError):
    pass


class WorkspaceStore(Protocol):
    def load(self) -> tuple[int, bytes | None]: ...
    def save(self, payload: bytes, expected_revision: int) -> int: ...


def _find(items: list, identifier: str):
    for item in items:
        if item.id == identifier:
            return item
    raise MissingRecord("That item is no longer present. Reload the workspace.")


def _within(reference: DocumentSelection, ranges: list[DocumentSelection]) -> bool:
    return any(r.document_id == reference.document_id and r.start_line <= reference.start_line <= reference.end_line <= r.end_line for r in ranges)


def selected_documents(workspace: Workspace, ranges: list[DocumentSelection]) -> list[ContextDocument]:
    passages = []
    for reference in ranges:
        document = _find(workspace.documents, reference.document_id)
        lines = document.text.split("\n")
        if reference.end_line > len(lines):
            raise UserError("Selected lines are outside the saved document. Choose the excerpt again.")
        passages.append(ContextDocument(
            **reference.model_dump(), filename=document.filename, text_sha256=document.text_sha256,
            text="\n".join(lines[reference.start_line - 1:reference.end_line]),
        ))
    return passages


def select_context(workspace: Workspace, selection: ContextSelection) -> ContextBundle:
    messages = {item.id: item for item in workspace.messages}
    statements = {item.id: item for item in workspace.statements}
    message_ids, statement_ids = set(selection.message_ids), set(selection.statement_ids)
    if not message_ids <= messages.keys() or not statement_ids <= statements.keys():
        raise UserError("Selected context is no longer available. Reload and select it again.")
    documents = selected_documents(workspace, selection.document_ranges)
    if not message_ids and not statement_ids and not documents:
        raise UserError("Select at least one saved message, Person Map statement, or document excerpt.")
    # Selecting a statement does not implicitly disclose quotes from unselected
    # conversations or links to unselected statements.
    selected_statements = []
    for item in workspace.statements:
        if item.id in statement_ids:
            selected_statements.append(item.model_copy(update={
                "evidence": [e for e in item.evidence if e.message_id in message_ids],
                "document_evidence": [e for e in item.document_evidence if _within(e, selection.document_ranges)],
                "related_statement_ids": [s for s in item.related_statement_ids if s in statement_ids],
            }))
    return ContextBundle(
        task=selection.task,
        messages=[m.model_copy(update={
            "document_ranges": [r for r in m.document_ranges if _within(r, selection.document_ranges)],
        }) for m in workspace.messages if m.id in message_ids],
        statements=selected_statements,
        documents=documents,
    )


def require_chat_message(workspace: Workspace, message_id: str) -> None:
    if not workspace.messages or workspace.messages[-1].id != message_id or workspace.messages[-1].role != "user":
        raise ConflictError("Chat requires the latest unanswered user message. Reload before trying again.")
    if len(workspace.messages) >= 500:
        raise UserError("The conversation is at its saved-message limit. Export your workspace before starting a new one.")


def validate_reply(reply: ModelReply, context: ContextBundle) -> None:
    user_messages = {m.id: m.text for m in context.messages if m.role == "user"}
    statement_ids = {s.id for s in context.statements}
    for draft in reply.proposed_statements:
        if draft.source == "explicit" and not draft.evidence:
            raise UserError("An explicit model assertion needs an exact quote from a selected user message.")
        if draft.source != "explicit" and draft.flexibility == "hard_limit":
            raise UserError("An inference or document claim cannot establish a hard limit. Edit or clarify it first.")
        if draft.source == "document" and not draft.document_evidence:
            raise UserError("A document claim needs an exact quote from a selected document excerpt.")
        for evidence in draft.document_evidence:
            passage = next((p for p in context.documents if _within(evidence, [p])), None)
            if passage is None:
                raise UserError("Proposed document evidence is outside the selected excerpts.")
            cited = "\n".join(passage.text.split("\n")[evidence.start_line - passage.start_line:evidence.end_line - passage.start_line + 1])
            if evidence.quote not in cited:
                raise UserError("Proposed document evidence must quote its selected lines exactly.")
        for evidence in draft.evidence:
            if evidence.message_id not in user_messages or evidence.quote not in user_messages[evidence.message_id]:
                raise UserError("Proposed evidence must quote a selected user message exactly.")
        if not set(draft.related_statement_ids) <= statement_ids:
            raise UserError("A proposal links to a statement outside the selected context.")
    for opportunity in reply.opportunities:
        if not set(opportunity.statement_ids) <= statement_ids:
            raise UserError("An opportunity links to a statement outside the selected context.")
    if context.task == "brief" and reply.brief is None:
        raise UserError("The model did not return a brief. Try again or correct the JSON reply.")
    if context.task == "discover" and not reply.opportunities:
        raise UserError("The model did not return possibilities. Try again with more useful context.")


def hard_gates(workspace: Workspace) -> list[str]:
    # Model-authored free text cannot silently turn a preference into a hard gate.
    return [
        s.text + (f" (Condition: {s.condition})" if s.condition else "")
        for s in workspace.statements if s.confirmed and s.flexibility == "hard_limit"
    ]


class Bench:
    def __init__(self, store: WorkspaceStore):
        self.store = store

    def load(self, expected_revision: int | None = None) -> tuple[int, Workspace]:
        revision, payload = self.store.load()
        if expected_revision is not None and expected_revision != revision:
            raise ConflictError("The workspace changed. Reload it and prepare a fresh request.")
        try:
            workspace = Workspace() if payload is None else Workspace.model_validate_json(payload)
        except ValidationError:
            raise StorageError("Stored workspace data is invalid or unsupported; nothing was reset.") from None
        return revision, workspace

    def _save(self, workspace: Workspace, revision: int) -> tuple[int, Workspace]:
        try:
            # List mutations and model_copy do not themselves re-run Pydantic rules.
            checked = Workspace.model_validate(workspace.model_dump())
        except ValidationError:
            raise UserError("This change exceeds a workspace limit or contains invalid references. Nothing was saved.") from None
        next_revision = self.store.save(checked.model_dump_json().encode("utf-8"), revision)
        return next_revision, checked

    def context(self, revision: int, selection: ContextSelection) -> ContextBundle:
        _, workspace = self.load(revision)
        return select_context(workspace, selection)

    def add_message(self, revision: int, text: str, document_ranges: list[DocumentSelection] | None = None) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        selected_documents(workspace, document_ranges or [])
        workspace.messages.append(Message(role="user", text=text, document_ranges=document_ranges or []))
        return self._save(workspace, revision)

    def add_document(self, request: DocumentSaveRequest) -> tuple[int, Workspace]:
        _, workspace = self.load(request.revision)
        text = checked_text(request.text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if any(d.text_sha256 == digest for d in workspace.documents):
            raise UserError("This exact text is already saved. Use its existing source, or correct the preview before saving a new snapshot.")
        workspace.documents.append(DocumentSource(
            filename=request.filename, format=request.format, text=text, text_sha256=digest,
        ))
        return self._save(workspace, request.revision)

    def document_impact(self, identifier: str) -> dict:
        revision, workspace = self.load()
        _find(workspace.documents, identifier)
        return {
            "revision": revision,
            "statement_ids": [s.id for s in workspace.statements if any(e.document_id == identifier for e in s.document_evidence)],
            "proposal_count": len(workspace.proposals), "opportunity_count": len(workspace.opportunities),
            "has_brief": workspace.brief is not None,
            "message_count": sum(any(r.document_id == identifier for r in m.document_ranges) for m in workspace.messages),
        }

    def delete_document(self, revision: int, identifier: str) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        _find(workspace.documents, identifier)
        removed = {s.id for s in workspace.statements if any(e.document_id == identifier for e in s.document_evidence)}
        workspace.documents = [d for d in workspace.documents if d.id != identifier]
        workspace.statements = [s for s in workspace.statements if s.id not in removed]
        for statement in workspace.statements:
            statement.related_statement_ids = [i for i in statement.related_statement_ids if i not in removed]
        for message in workspace.messages:
            message.document_ranges = [r for r in message.document_ranges if r.document_id != identifier]
        # Free-text artifacts can repeat source claims without a formal citation.
        # Conversation remains; the UI explicitly discloses this separate lifetime.
        workspace.proposals = []
        workspace.opportunities = []
        workspace.brief = None
        workspace.map_version += 1
        return self._save(workspace, revision)

    def answer_chat(self, revision: int, message_id: str, text: str) -> tuple[int, Workspace]:
        """Commit one complete answer, never proposals, against the original revision."""
        _, workspace = self.load(revision)
        require_chat_message(workspace, message_id)
        try:
            # Bound the raw answer too: whitespace must not hide an oversized reply.
            if not isinstance(text, str) or len(text) > 12000:
                raise ValueError
            text.encode("utf-8")
            answer = Message(role="assistant", text=text)
        except (ValueError, UnicodeError):
            raise UserError("The chat reply was empty, too long, or invalid. No assistant reply was saved.") from None
        workspace.messages.append(answer)
        return self._save(workspace, revision)

    def save_statement(self, revision: int, draft: StatementDraft, identifier: str | None = None) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        values = draft.model_dump()
        if identifier is None:
            workspace.statements.append(Statement(**values, confirmed=True, origin="user entry"))
        else:
            original = _find(workspace.statements, identifier)
            origin = original.origin
            if not origin.endswith("; user-edited"):
                origin = origin[:280] + "; user-edited"
            replacement = Statement(
                **values, id=identifier, confirmed=True, origin=origin,
                created_at=original.created_at, updated_at=now(),
            )
            workspace.statements = [replacement if s.id == identifier else s for s in workspace.statements]
        workspace.map_version += 1
        return self._save(workspace, revision)

    def delete_statement(self, revision: int, identifier: str) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        _find(workspace.statements, identifier)
        workspace.statements = [s for s in workspace.statements if s.id != identifier]
        for statement in workspace.statements:
            statement.related_statement_ids = [s for s in statement.related_statement_ids if s != identifier]
        workspace.map_version += 1
        # Free-text derived artifacts can repeat deleted information even without
        # a formal link. Conversation is separate and requires full reset to erase.
        workspace.opportunities = []
        workspace.proposals = []
        workspace.brief = None
        return self._save(workspace, revision)

    def propose(self, revision: int, selection: ContextSelection, reply: ModelReply, origin: str) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        context = select_context(workspace, selection)
        reply = reply.model_copy(deep=True)
        allowed_ids = {statement.id for statement in context.statements}
        unverified_indices = []
        for index, opportunity in enumerate(reply.opportunities):
            if not set(opportunity.statement_ids) <= allowed_ids:
                # These are optional hypothesis links, not personal-fact evidence.
                # Never guess a replacement or look outside the selected context.
                # Retain a durable warning for review, saved results, and exports.
                opportunity.statement_ids = [identifier for identifier in opportunity.statement_ids if identifier in allowed_ids]
                unverified_indices.append(index)
        validate_reply(reply, context)
        if reply.brief is not None:
            reply.brief.hard_gates = hard_gates(workspace)
        workspace.messages.append(Message(role="assistant", text=reply.assistant_text))
        if reply.proposed_statements or reply.opportunities or reply.brief is not None:
            workspace.proposals.append(Proposal(
                reply=reply, selection=selection, based_on_map_version=workspace.map_version, origin=origin,
                unverified_opportunity_indices=unverified_indices,
            ))
        return self._save(workspace, revision)

    def accept(self, identifier: str, request: AcceptRequest) -> tuple[int, Workspace]:
        _, workspace = self.load(request.revision)
        proposal = _find(workspace.proposals, identifier)
        if proposal.based_on_map_version != workspace.map_version:
            raise ConflictError("The Person Map changed after this proposal. Reject it and prepare a fresh request.")
        validate_reply(proposal.reply, select_context(workspace, proposal.selection))
        for indices, drafts in (
            (request.statement_indices, proposal.reply.proposed_statements),
            (request.opportunity_indices, proposal.reply.opportunities),
        ):
            if len(indices) != len(set(indices)) or any(type(i) is not int or i < 0 or i >= len(drafts) for i in indices):
                raise UserError("Select each valid proposal item at most once.")
        if request.include_brief and proposal.reply.brief is None:
            raise UserError("This proposal does not contain a brief.")
        if not request.statement_indices and not request.opportunity_indices and not request.include_brief:
            raise UserError("Choose at least one item to accept, or reject the proposal.")
        for index in request.statement_indices:
            draft = proposal.reply.proposed_statements[index]
            workspace.statements.append(Statement(
                **draft.model_dump(), confirmed=True, origin=proposal.origin,
            ))
        if request.statement_indices:
            workspace.map_version += 1
        for index in request.opportunity_indices:
            workspace.opportunities.append(Opportunity(
                **proposal.reply.opportunities[index].model_dump(), origin=proposal.origin,
                based_on_map_version=workspace.map_version,
                unverified_links=index in proposal.unverified_opportunity_indices,
            ))
        if request.include_brief:
            values = proposal.reply.brief.model_dump()
            values["hard_gates"] = hard_gates(workspace)
            workspace.brief = Brief(**values, origin=proposal.origin, based_on_map_version=workspace.map_version)
        workspace.proposals = [p for p in workspace.proposals if p.id != identifier]
        return self._save(workspace, request.revision)

    def reject(self, revision: int, identifier: str) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        _find(workspace.proposals, identifier)
        workspace.proposals = [p for p in workspace.proposals if p.id != identifier]
        return self._save(workspace, revision)

    def feedback(self, revision: int, identifier: str, feedback: str, note: str) -> tuple[int, Workspace]:
        _, workspace = self.load(revision)
        opportunity = _find(workspace.opportunities, identifier)
        opportunity.feedback, opportunity.feedback_note = feedback, note
        workspace.messages.append(Message(
            role="user", text=f"My feedback on '{opportunity.title}': {feedback.replace('_', ' ')}. {note}".strip(),
        ))
        # Feedback is evidence for the next conversation, never an automatic
        # inferred preference or silent change to the Person Map.
        return self._save(workspace, revision)

    def replace(self, revision: int, workspace: Workspace) -> tuple[int, Workspace]:
        self.load(revision)
        for proposal in workspace.proposals:
            validate_reply(proposal.reply, select_context(workspace, proposal.selection))
        return self._save(workspace, revision)

    def reset(self, revision: int) -> tuple[int, Workspace]:
        self.load(revision)
        return self._save(Workspace(), revision)


def _markdown(text: str) -> str:
    # Keep model-authored HTML, images and links inert in the portable report.
    escaped = html.escape(text, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", escaped)


def _markdown_list(values: list[str], *, ordered: bool = False) -> list[str]:
    lines = []
    for index, value in enumerate(values, 1):
        prefix = f"{index}. " if ordered else "- "
        lines.append(prefix + _markdown(value).replace("\n", "\n" + " " * len(prefix)))
    return lines or ["Not established yet."]


def render_brief(workspace: Workspace) -> str:
    brief = workspace.brief
    if brief is None:
        raise UserError("Generate and accept a Search Brief first.")
    lines = [
        f"# {_markdown(brief.title)}", "", "Private Pip insight and exploration brief", "",
        f"Scope: {workspace.geography}", "",
        "Possibilities are hypotheses, not verified openings, financial advice, or established world facts.",
        "This plaintext export contains private information. Share it deliberately.", "",
        f"Origin: {_markdown(brief.origin)} | Created: {brief.created_at.isoformat()}", "",
    ]
    if brief.based_on_map_version != workspace.map_version:
        lines.extend(["> STALE: the Person Map changed after this brief was created. Regenerate before relying on it.", ""])
    lines.extend(["## What matters and what differs", "", _markdown(brief.summary), ""])
    lines.extend(["## Reviewed hard gates", "", *_markdown_list(brief.hard_gates), ""])
    lines.extend(["## Paths to investigate", ""])
    for index, direction in enumerate(brief.directions, 1):
        lines.extend([f"### Path {index}", "", _markdown(direction), ""])
    if not brief.directions:
        lines.extend(["No paths listed yet. This does not rule out other possibilities.", ""])
    for heading, values, ordered in (
        ("Questions that could change the choice", brief.open_questions, False),
        ("A small investigation plan", brief.next_steps, True),
        ("Search vocabulary and queries", brief.search_queries, False),
    ):
        lines.extend([f"## {heading}", "", *_markdown_list(values, ordered=ordered), ""])
    lines.extend(["## Current Person Map", ""])
    for domain, label in DOMAINS.items():
        items = [s for s in workspace.statements if s.domain == domain]
        if not items:
            continue
        lines.extend([f"### {label}", ""])
        for item in items:
            lines.extend([
                "- " + _markdown(item.text),
                (
                    f"  - Source: {item.source}; user-reviewed: {'yes' if item.confirmed else 'no'}; "
                    f"confidence: {item.confidence}; flexibility: {item.flexibility}; importance: {item.importance}."
                ),
            ])
            for evidence in item.document_evidence:
                document = _find(workspace.documents, evidence.document_id)
                lines.append(f"  - Document: {_markdown(document.filename)}, lines {evidence.start_line}–{evidence.end_line} (snapshot {document.id}). Claim from a document, not independently verified.")
            if item.condition:
                lines.append("  - Condition: " + _markdown(item.condition))
            if item.tension:
                lines.append("  - Tension: " + _markdown(item.tension))
        lines.append("")
    if workspace.opportunities:
        lines.extend([
            "## Saved possibilities — separate reference", "",
            "These saved workspace hypotheses were not automatically supplied to the brief's selected context.", "",
        ])
        statements = {statement.id: statement for statement in workspace.statements}
        for item in workspace.opportunities:
            lines.extend([f"### {_markdown(item.title)}", "", "Why this path may fit:", "", _markdown(item.rationale), ""])
            if item.unverified_links:
                lines.extend([
                    (
                        "Warning: unverified Person Map links were omitted from this possibility. "
                        "Review its rationale; no replacement evidence was inferred."
                    ), "",
                ])
            if item.based_on_map_version != workspace.map_version:
                lines.extend(["Map changed since this hypothesis was generated.", ""])
            for label, values, ordered in (
                ("Tradeoffs to consider", item.tradeoffs, False),
                ("Questions and unknowns that matter", item.unknowns, False),
                ("A way in · small investigations", item.next_steps, True),
            ):
                lines.extend([f"{label}:", "", *_markdown_list(values, ordered=ordered), ""])
            references = [f"{statements[identifier].text} (ID: {identifier})" for identifier in item.statement_ids]
            lines.extend(["Person Map connections (current notes):", ""])
            lines.extend(_markdown_list(references) if references else ["No Person Map references supplied."])
            lines.extend(["", f"Feedback: {item.feedback.replace('_', ' ')}", _markdown(item.feedback_note), ""])
    return "\n".join(lines) + "\n"
