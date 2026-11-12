"""Versioned private state and the deliberately smaller model-output contract."""

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .context_limits import DEFAULT_CONTEXT_TOKENS, ContextTokens

Domain = Literal["background", "work", "environment", "life", "place", "money", "direction"]
DOMAINS: dict[str, str] = {
    "background": "What You Bring",
    "work": "The Work",
    "environment": "How You Work",
    "life": "Your Life",
    "place": "Your Place",
    "money": "The Numbers",
    "direction": "Where You're Going",
}
Geography = Literal["Contiguous United States: the 48 adjoining states and Washington, DC"]
GEOGRAPHY: Geography = "Contiguous United States: the 48 adjoining states and Washington, DC"
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-f0-9-]{36}$")]
Task = Literal["conversation", "discover", "brief"]


def new_id() -> str:
    return str(uuid4())


def now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def utf8_text(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Unsupported text encoding") from None
    return value


DocumentFormat = Literal["pdf", "docx", "txt", "md", "paste"]
DocumentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^/\\:\x00-\x1f\x7f]+$"), AfterValidator(utf8_text)]


class DocumentSelection(StrictModel):
    document_id: Identifier
    start_line: int = Field(ge=1, le=2000, strict=True)
    end_line: int = Field(ge=1, le=2000, strict=True)

    @model_validator(mode="after")
    def ordered_lines(self) -> "DocumentSelection":
        if self.end_line < self.start_line:
            raise ValueError("Document line range is reversed")
        return self


def check_document_ranges(ranges: list[DocumentSelection]) -> None:
    for index, current in enumerate(ranges):
        for previous in ranges[:index]:
            if current.document_id == previous.document_id and max(current.start_line, previous.start_line) <= min(current.end_line, previous.end_line):
                raise ValueError("Selected document ranges must not overlap")


class DocumentSource(StrictModel):
    # A new save creates a new immutable text snapshot, never edits an old citation.
    id: Identifier = Field(default_factory=new_id)
    filename: DocumentName
    format: DocumentFormat
    text: str = Field(min_length=1, max_length=60000)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def valid_snapshot(self) -> "DocumentSource":
        if len(self.text.split("\n")) > 2000 or any(ord(c) < 32 and c not in "\n\t" for c in self.text):
            raise ValueError("Document text exceeds line limits or contains control characters")
        try:
            self.filename.encode("utf-8")
            digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        except UnicodeError:
            raise ValueError("Unsupported document text encoding") from None
        if digest != self.text_sha256:
            raise ValueError("Document text does not match its snapshot hash")
        return self


class DocumentEvidence(DocumentSelection):
    quote: Text


class ContextDocument(DocumentSelection):
    filename: DocumentName
    text_sha256: str
    # Preserve blank lines/indentation: locators refer to the saved snapshot.
    text: Annotated[str, StringConstraints(strip_whitespace=False)]


class Message(StrictModel):
    id: Identifier = Field(default_factory=new_id)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=12000)
    created_at: datetime = Field(default_factory=now)
    document_ranges: list[DocumentSelection] = Field(default_factory=list, max_length=8)


class Evidence(StrictModel):
    message_id: Identifier
    quote: Text = Field(description="An exact contiguous substring copied from the referenced user message. No paraphrase, ellipsis, bracketed omissions, or changed punctuation.")


class StatementDraft(StrictModel):
    text: Text
    domain: Domain
    dimension: ShortText = "General"
    facet: ShortText = "Observation"
    importance: Literal["low", "medium", "high"] = "medium"
    flexibility: Literal["hard_limit", "strong_preference", "preference", "exploration"] = "preference"
    source: Literal["explicit", "inferred", "document"] = "inferred"
    confidence: Literal["tentative", "supported"] = "tentative"
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    document_evidence: list[DocumentEvidence] = Field(default_factory=list, max_length=8)
    condition: Text | None = None
    time_horizon: Literal["current", "near_term", "long_term", "unspecified"] = "unspecified"
    related_statement_ids: list[Identifier] = Field(default_factory=list, max_length=12)
    tension: Text | None = None


class Statement(StatementDraft):
    id: Identifier = Field(default_factory=new_id)
    confirmed: bool = False
    origin: ShortText = "user"
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class OpportunityDraft(StrictModel):
    title: ShortText
    kind: Literal["career", "place", "training", "lifestyle", "employer", "transition", "job", "other"]
    rationale: Text
    statement_ids: list[Identifier] = Field(default_factory=list, max_length=12)
    tradeoffs: list[Text] = Field(default_factory=list, max_length=8)
    unknowns: list[Text] = Field(default_factory=list, max_length=8)
    next_steps: list[Text] = Field(default_factory=list, max_length=8)


class Opportunity(OpportunityDraft):
    id: Identifier = Field(default_factory=new_id)
    status: Literal["hypothesis"] = "hypothesis"
    unverified_links: bool = False
    origin: ShortText
    based_on_map_version: int = Field(ge=0)
    feedback: Literal["unreviewed", "worth_exploring", "not_for_me", "unsure"] = "unreviewed"
    feedback_note: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=now)


class BriefDraft(StrictModel):
    title: ShortText
    summary: str = Field(min_length=1, max_length=6000)
    hard_gates: list[Annotated[str, StringConstraints(min_length=1, max_length=4200)]] = Field(default_factory=list, max_length=200)
    directions: list[Text] = Field(default_factory=list, max_length=12)
    search_queries: list[ShortText] = Field(default_factory=list, max_length=12)
    open_questions: list[Text] = Field(default_factory=list, max_length=12)
    next_steps: list[Text] = Field(default_factory=list, max_length=12)


class Brief(BriefDraft):
    origin: ShortText
    based_on_map_version: int = Field(ge=0)
    created_at: datetime = Field(default_factory=now)


class ModelReply(StrictModel):
    assistant_text: str = Field(min_length=1, max_length=12000)
    proposed_statements: list[StatementDraft] = Field(default_factory=list, max_length=12)
    opportunities: list[OpportunityDraft] = Field(default_factory=list, max_length=6)
    brief: BriefDraft | None = None


class ContextSelection(StrictModel):
    task: Task = "conversation"
    message_ids: list[Identifier] = Field(default_factory=list, max_length=24)
    statement_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    document_ranges: list[DocumentSelection] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_selections(self) -> "ContextSelection":
        if len(set(self.message_ids)) != len(self.message_ids) or len(set(self.statement_ids)) != len(self.statement_ids):
            raise ValueError("Context IDs must be unique")
        check_document_ranges(self.document_ranges)
        return self


class ContextBundle(StrictModel):
    task: Task
    # Runtime tuning is not personal context and must stay out of prompt exports.
    context_tokens: ContextTokens = Field(default=DEFAULT_CONTEXT_TOKENS, exclude=True)
    geography: Geography = GEOGRAPHY
    messages: list[Message]
    statements: list[Statement]
    documents: list[ContextDocument] = Field(default_factory=list)


class Proposal(StrictModel):
    id: Identifier = Field(default_factory=new_id)
    reply: ModelReply
    selection: ContextSelection
    based_on_map_version: int = Field(ge=0)
    origin: ShortText
    created_at: datetime = Field(default_factory=now)
    # Application-owned review metadata, deliberately absent from ModelReply.
    unverified_opportunity_indices: list[Annotated[int, Field(ge=0, strict=True)]] = Field(default_factory=list, max_length=6)
    repaired_document_statement_indices: list[Annotated[int, Field(ge=0, strict=True)]] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_warning_indices(self) -> "Proposal":
        indices = self.unverified_opportunity_indices
        if len(indices) != len(set(indices)) or any(i >= len(self.reply.opportunities) for i in indices):
            raise ValueError("Link warnings must identify distinct opportunities in this proposal")
        indices = self.repaired_document_statement_indices
        if len(indices) != len(set(indices)) or any(i >= len(self.reply.proposed_statements) for i in indices):
            raise ValueError("Citation repair warnings must identify distinct statements in this proposal")
        return self


class Workspace(StrictModel):
    schema_version: Literal[2] = 2
    geography: Geography = GEOGRAPHY
    map_version: int = Field(default=0, ge=0)
    messages: list[Message] = Field(default_factory=list, max_length=500)
    statements: list[Statement] = Field(default_factory=list, max_length=200)
    opportunities: list[Opportunity] = Field(default_factory=list, max_length=100)
    proposals: list[Proposal] = Field(default_factory=list, max_length=10)
    brief: Brief | None = None
    documents: list[DocumentSource] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def migrate_v1(cls, value):
        # Upgrade in memory; the next ordinary revisioned save persists v2. Reading
        # an old database never resets it or writes a migration behind the user's back.
        if isinstance(value, dict) and value.get("schema_version") == 1:
            if value.get("documents"):
                raise ValueError("Document sources require workspace format 2")
            return {**value, "schema_version": 2, "documents": []}
        return value

    @model_validator(mode="after")
    def validate_references(self) -> "Workspace":
        collections = [self.messages, self.statements, self.opportunities, self.proposals, self.documents]
        for collection in collections:
            ids = [item.id for item in collection]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate record IDs are not allowed")
        messages = {message.id: message for message in self.messages}
        statement_ids = {statement.id for statement in self.statements}
        documents = {document.id: document for document in self.documents}

        def document_text(reference: DocumentSelection) -> str:
            document = documents.get(reference.document_id)
            if document is None or reference.end_line > len(document.text.split("\n")):
                raise ValueError("Document reference is missing or outside its text")
            return "\n".join(document.text.split("\n")[reference.start_line - 1:reference.end_line])

        for message in self.messages:
            check_document_ranges(message.document_ranges)
            if message.role != "user" and message.document_ranges:
                raise ValueError("Only user messages may explicitly attach document excerpts")
            for reference in message.document_ranges:
                document_text(reference)
        for statement in self.statements:
            if statement.source == "document" and not statement.document_evidence:
                raise ValueError("A document claim needs document evidence")
            if statement.source == "explicit" and statement.document_evidence and not statement.evidence:
                raise ValueError("A document citation is not a direct user quotation")
            for evidence in statement.document_evidence:
                if evidence.quote not in document_text(evidence):
                    raise ValueError("Document evidence must quote its cited lines exactly")
            for evidence in statement.evidence:
                message = messages.get(evidence.message_id)
                if message is None or message.role != "user" or evidence.quote not in message.text:
                    raise ValueError("Statement evidence must quote an existing user message")
            if not set(statement.related_statement_ids) <= statement_ids - {statement.id}:
                raise ValueError("Statement relationships must refer to other existing statements")
        derived = [*self.opportunities, *self.proposals]
        if self.brief is not None:
            derived.append(self.brief)
        if any(item.based_on_map_version > self.map_version for item in derived):
            raise ValueError("Derived output cannot refer to a future map version")
        for opportunity in self.opportunities:
            if not set(opportunity.statement_ids) <= statement_ids:
                raise ValueError("Opportunity refers to a missing statement")
        for proposal in self.proposals:
            if not set(proposal.selection.message_ids) <= set(messages):
                raise ValueError("Proposal refers to a missing message")
            if not set(proposal.selection.statement_ids) <= statement_ids:
                raise ValueError("Proposal refers to a missing statement")
            for reference in proposal.selection.document_ranges:
                document_text(reference)
            for draft in proposal.reply.proposed_statements:
                if draft.source == "document" and not draft.document_evidence:
                    raise ValueError("A proposed document claim needs document evidence")
                if draft.source == "explicit" and draft.document_evidence and not draft.evidence:
                    raise ValueError("A document citation is not a direct user quotation")
                for evidence in draft.document_evidence:
                    if evidence.quote not in document_text(evidence):
                        raise ValueError("Proposed document evidence is invalid")
                    if not any(r.document_id == evidence.document_id and r.start_line <= evidence.start_line <= evidence.end_line <= r.end_line for r in proposal.selection.document_ranges):
                        raise ValueError("Proposed evidence is outside the selected document context")
        return self


class RevisionRequest(StrictModel):
    revision: int = Field(ge=0, strict=True)


class MessageRequest(RevisionRequest):
    text: str = Field(min_length=1, max_length=12000)
    document_ranges: list[DocumentSelection] = Field(default_factory=list, max_length=8)


class DocumentPreviewRequest(StrictModel):
    filename: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(min_length=1, max_length=2796204)


class DocumentSaveRequest(RevisionRequest):
    filename: DocumentName
    format: DocumentFormat
    text: str = Field(min_length=1, max_length=60000)


class DocumentDeleteRequest(RevisionRequest):
    confirmation: Literal["DELETE"]


class StatementRequest(RevisionRequest):
    statement: StatementDraft


class ContextRequest(RevisionRequest):
    selection: ContextSelection
    context_tokens: ContextTokens = DEFAULT_CONTEXT_TOKENS


class GenerateRequest(ContextRequest):
    model: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")


class ChatRequest(RevisionRequest):
    model: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
    message_id: Identifier
    context_tokens: ContextTokens = DEFAULT_CONTEXT_TOKENS


class ManualReplyRequest(ContextRequest):
    reply_text: str = Field(min_length=1, max_length=100000)


class AcceptRequest(RevisionRequest):
    statement_indices: list[Annotated[int, Field(ge=0, strict=True)]] = Field(default_factory=list, max_length=12)
    opportunity_indices: list[Annotated[int, Field(ge=0, strict=True)]] = Field(default_factory=list, max_length=6)
    include_brief: bool = False


class FeedbackRequest(RevisionRequest):
    feedback: Literal["worth_exploring", "not_for_me", "unsure"]
    note: str = Field(default="", max_length=2000)


class ImportRequest(RevisionRequest):
    workspace: Workspace
    confirmation: Literal["REPLACE"]


class ResetRequest(RevisionRequest):
    confirmation: Literal["DELETE"]
