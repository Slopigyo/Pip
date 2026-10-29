"""Synthetic résumé intake tests. Never read real résumés or the user's workspace."""

import base64
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from jobwolf.app import create_app
from jobwolf.conversation import chat_input_bytes, chat_messages, select_chat_context
from jobwolf.documents import (
    MAX_FILE_BYTES,
    DocumentError,
    checked_text,
    extract_text,
    preview_document,
)
from jobwolf.models import (
    AcceptRequest,
    Brief,
    ContextSelection,
    DocumentEvidence,
    DocumentSaveRequest,
    DocumentSelection,
    Evidence,
    Message,
    ModelReply,
    Opportunity,
    Statement,
    StatementDraft,
    Workspace,
)
from jobwolf.providers import _reply_schema, build_prompt, structured_input_size
from jobwolf.service import Bench, UserError, render_brief, select_context, validate_reply
from jobwolf.storage import ConflictError, SQLiteStore
from test_app import TOKEN, SyntheticProvider
from test_service import MemoryStore


OMITTED = "SYNTHETIC_CONTACT_DO_NOT_SEND"
TEXT = OMITTED + "\n\nRepaired bicycles at a community workshop.\nBuilt a tool rack.\nUnselected final line."


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def pdf_bytes(*, blank=False, encrypted=False, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=600, height=800)
        if not blank:
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 30 750 Td (Synthetic bicycle repair) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("synthetic-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def docx_bytes(body=None, extra=None):
    output = io.BytesIO()
    ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    body = body or f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>Synthetic résumé</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Bicycle repair</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", body)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return output.getvalue()


class ParserTests(unittest.TestCase):
    def test_text_markdown_bom_and_worker_filename(self):
        for format, data in [("txt", b"\xef\xbb\xbfSynthetic\r\nrepair"), ("md", "# Résumé\nRepair".encode()), ("txt", "Synthetic résumé".encode("utf-16"))]:
            with self.subTest(format=format, data=data[:5]):
                self.assertTrue(extract_text(data, format)["text"])
        preview = preview_document(r"C:\private\résumé.md", encoded(b"# Synthetic resume\nRepair"))
        self.assertEqual(preview["filename"], "résumé.md")
        self.assertEqual(preview["text"], "# Synthetic resume\nRepair")

    def test_text_pdf_and_docx_extract_without_models(self):
        self.assertIn("bicycle repair", preview_document("synthetic.pdf", encoded(pdf_bytes()))["text"])
        parsed = extract_text(docx_bytes(), "docx")
        self.assertEqual(parsed["text"], "Synthetic résumé\nBicycle repair")
        self.assertTrue(parsed["warnings"])

    def test_pdf_scanned_encrypted_page_limits_and_malformed_errors(self):
        for data, expected in [(pdf_bytes(blank=True), "No readable text"), (pdf_bytes(encrypted=True), "Password-protected"), (pdf_bytes(pages=21), "1–20 pages"), (b"not a pdf PRIVATE_SENTINEL", "could not be read safely")]:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(DocumentError, expected) as error:
                    preview_document("synthetic.pdf", encoded(data))
                self.assertNotIn("PRIVATE_SENTINEL", str(error.exception))

    def test_archive_entities_macros_and_expansion_limits_are_rejected(self):
        cases = [
            docx_bytes('<!DOCTYPE a [<!ENTITY x "private">]><a>&x;</a>'),
            docx_bytes(extra={"word/vbaProject.bin": b"not executable"}),
            docx_bytes(extra={"large.bin": b"0" * (8 * 1024 * 1024 + 1)}),
        ]
        for data in cases:
            with self.subTest(size=len(data)), self.assertRaises(DocumentError):
                extract_text(data, "docx")

    def test_size_encoding_and_input_limits(self):
        for filename, data in [("bad.doc", encoded(b"a")), ("bad.txt", "not-base64!"), ("empty.txt", ""), ("large.txt", encoded(b"a" * (MAX_FILE_BYTES + 1)))]:
            with self.subTest(filename=filename), self.assertRaises(DocumentError):
                preview_document(filename, data)
        for text in [" ", "x" * 60001, "line\n" * 2001, "x\x00y", "x\ud800"]:
            with self.subTest(length=len(text)), self.assertRaises(DocumentError):
                checked_text(text)

    def test_timeout_is_bounded_and_never_passes_content_in_arguments(self):
        with (
            patch("jobwolf.documents.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 15)) as run,
            self.assertRaisesRegex(DocumentError, "took too long"),
        ):
            preview_document("synthetic.txt", encoded(b"SYNTHETIC_PRIVATE_TEXT"))
        args, kwargs = run.call_args
        self.assertNotIn("SYNTHETIC_PRIVATE_TEXT", str(args))
        self.assertEqual(kwargs["input"], b"SYNTHETIC_PRIVATE_TEXT")
        self.assertEqual(kwargs["timeout"], 15)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.bench = Bench(self.store)
        self.revision, self.workspace = self.bench.add_document(DocumentSaveRequest(revision=0, filename="Synthetic résumé.txt", format="txt", text=TEXT))
        self.document = self.workspace.documents[0]
        self.range = DocumentSelection(document_id=self.document.id, start_line=2, end_line=4)
        self.selection = ContextSelection(document_ranges=[self.range])
        self.evidence = DocumentEvidence(document_id=self.document.id, start_line=3, end_line=3, quote="Repaired bicycles at a community workshop.")
        self.reply = ModelReply(assistant_text="What parts did you enjoy?", proposed_statements=[StatementDraft(
            text="The résumé describes bicycle repair experience", domain="background", source="document", document_evidence=[self.evidence],
        )])

    def test_selected_excerpts_preserve_blank_lines_and_exact_citations(self):
        context = select_context(self.workspace, self.selection)
        self.assertEqual(context.documents[0].text, "\nRepaired bicycles at a community workshop.\nBuilt a tool rack.")
        self.assertNotIn(OMITTED, context.model_dump_json())
        self.assertNotIn("Unselected final line", build_prompt(context))
        validate_reply(self.reply, context)
        self.assertTrue(structured_input_size(context).fits)
        schema = _reply_schema(context)
        self.assertIn("document", schema["$defs"]["StatementDraft"]["properties"]["source"]["enum"])
        self.assertEqual(schema["$defs"]["DocumentEvidence"]["properties"]["document_id"]["enum"], [self.document.id])

    def test_source_only_proposal_review_edit_and_roundtrip_preserve_provenance(self):
        revision, staged = self.bench.propose(self.revision, self.selection, self.reply, "synthetic")
        self.assertEqual(staged.statements, [])
        revision, accepted = self.bench.accept(staged.proposals[0].id, AcceptRequest(revision=revision, statement_indices=[0]))
        statement = accepted.statements[0]
        self.assertTrue(statement.confirmed)
        self.assertEqual(statement.source, "document")
        self.assertEqual(statement.document_evidence, [self.evidence])
        draft = self.reply.proposed_statements[0].model_copy(update={"text": "Reviewed description of bicycle repair"})
        revision, edited = self.bench.save_statement(revision, draft, statement.id)
        self.assertEqual(edited.statements[0].document_evidence, [self.evidence])
        restored = Workspace.model_validate_json(edited.model_dump_json())
        _, imported = self.bench.replace(revision, restored)
        self.assertEqual(imported, edited)
        imported.brief = Brief(title="Synthetic brief", summary="Explore", origin="synthetic", based_on_map_version=imported.map_version)
        report = render_brief(imported)
        self.assertIn("lines 3–3", report)
        self.assertNotIn(OMITTED, report)

    def test_document_id_in_message_evidence_is_reclassified_before_review(self):
        reply = self.reply.model_copy(deep=True)
        draft = reply.proposed_statements[0]
        draft.source = "explicit"
        draft.document_evidence = []
        draft.evidence = [Evidence(message_id=self.document.id, quote=self.evidence.quote)]
        original = reply.model_dump()
        revision, staged = self.bench.propose(self.revision, self.selection, reply, "synthetic")
        proposal = staged.proposals[0]
        corrected = proposal.reply.proposed_statements[0]
        self.assertEqual(corrected.source, "document")
        self.assertEqual(corrected.evidence, [])
        self.assertEqual(corrected.document_evidence, [self.evidence])
        self.assertEqual(proposal.repaired_document_statement_indices, [0])
        self.assertEqual(reply.model_dump(), original)
        self.assertFalse(staged.statements)
        self.assertTrue(all(message.role == "assistant" for message in staged.messages))
        _, accepted = self.bench.accept(proposal.id, AcceptRequest(revision=revision, statement_indices=[0]))
        self.assertEqual(accepted.statements[0].source, "document")
        self.assertEqual(accepted.statements[0].document_evidence, [self.evidence])
        self.assertEqual(Workspace.model_validate_json(accepted.model_dump_json()), accepted)

    def test_message_citation_schema_matches_only_selected_user_messages(self):
        user = Message(role="user", text="I no longer want repair work.")
        assistant = Message(role="assistant", text="What do you want to change?")
        unselected = Message(role="user", text="SYNTHETIC_UNSELECTED_MESSAGE")
        workspace = self.workspace.model_copy(update={"messages": [user, assistant, unselected]})
        for message_ids in ([], [assistant.id], [assistant.id, user.id]):
            with self.subTest(message_ids=message_ids):
                selection = self.selection.model_copy(update={"message_ids": message_ids})
                context = select_context(workspace, selection)
                schema = _reply_schema(context)
                properties = schema["$defs"]["StatementDraft"]["properties"]
                if user.id in message_ids:
                    self.assertIn("explicit", properties["source"]["enum"])
                    self.assertIn("evidence", properties)
                    self.assertEqual(schema["$defs"]["Evidence"]["properties"]["message_id"]["enum"], [user.id])
                else:
                    self.assertNotIn("explicit", properties["source"]["enum"])
                    self.assertNotIn("evidence", properties)
                    self.assertNotIn("Evidence", schema["$defs"])
                    self.assertIn("No user messages are selected", build_prompt(context))
                self.assertIn("document", properties["source"]["enum"])
                self.assertIn("inferred", properties["source"]["enum"])
                self.assertNotIn(unselected.id, build_prompt(context))

    def test_reclassified_citations_preserve_inference_and_do_not_duplicate_evidence(self):
        reply = self.reply.model_copy(deep=True)
        draft = reply.proposed_statements[0]
        draft.source = "inferred"
        draft.evidence = [Evidence(message_id=self.document.id, quote="Repaired  bicycles at a community workshop.")]
        _, staged = self.bench.propose(self.revision, self.selection, reply, "synthetic")
        corrected = staged.proposals[0].reply.proposed_statements[0]
        self.assertEqual(corrected.source, "inferred")
        self.assertEqual(corrected.evidence, [])
        self.assertEqual(corrected.document_evidence, [self.evidence])
        self.assertEqual(staged.proposals[0].repaired_document_statement_indices, [0])

    def test_reclassification_preserves_real_user_evidence_in_mixed_context(self):
        revision, workspace = self.bench.add_message(self.revision, "I no longer want repair work.")
        user = workspace.messages[-1]
        evidence = Evidence(message_id=user.id, quote=user.text)
        reply = self.reply.model_copy(deep=True)
        draft = reply.proposed_statements[0]
        draft.source = "explicit"
        draft.document_evidence = []
        draft.evidence = [Evidence(message_id=self.document.id, quote=self.evidence.quote), evidence]
        selection = self.selection.model_copy(update={"message_ids": [user.id]})
        _, staged = self.bench.propose(revision, selection, reply, "synthetic")
        corrected = staged.proposals[0].reply.proposed_statements[0]
        self.assertEqual(corrected.source, "explicit")
        self.assertEqual(corrected.evidence, [evidence])
        self.assertEqual(corrected.document_evidence, [self.evidence])

    def test_wrong_field_recovery_rejects_unverifiable_evidence_and_hard_limits_atomically(self):
        revision, workspace = self.bench.add_document(DocumentSaveRequest(
            revision=self.revision, filename="Unselected synthetic.txt", format="txt", text="Different source.",
        ))
        other_id = workspace.documents[-1].id
        before = self.store.load()
        for identifier, quote, flexibility, reason in (
            (Message(role="user", text="Unsaved").id, self.evidence.quote, "preference", "No user messages were selected"),
            (other_id, "Different source.", "preference", "No user messages were selected"),
            (self.document.id, "Invented credential", "preference", "not found"),
            (self.document.id, OMITTED, "preference", "not found"),
            (self.document.id, "a", "preference", "more than one location"),
            (self.document.id, self.evidence.quote, "hard_limit", "cannot establish a hard limit"),
        ):
            reply = self.reply.model_copy(deep=True)
            draft = reply.proposed_statements[0]
            draft.source = "explicit"
            draft.flexibility = flexibility
            draft.document_evidence = []
            draft.evidence = [Evidence(message_id=identifier, quote=quote)]
            with self.subTest(reason=reason), self.assertRaisesRegex(UserError, reason) as error:
                self.bench.propose(revision, self.selection, reply, "synthetic")
            if flexibility != "hard_limit":
                self.assertIn("Map note 1, message citation 1", str(error.exception))
            self.assertNotIn(self.document.filename, str(error.exception))
            self.assertNotIn(OMITTED, str(error.exception))
            self.assertEqual(self.store.load(), before)

    def test_reclassification_never_overrides_selected_message_identity(self):
        for role, text in (("user", self.evidence.quote), ("user", "Other user text"), ("assistant", self.evidence.quote)):
            with self.subTest(role=role, text=text):
                bench = Bench(MemoryStore())
                message = Message(id=self.document.id, role=role, text=text)
                workspace = self.workspace.model_copy(update={"messages": [message]})
                revision, _ = bench.replace(0, workspace)
                selection = self.selection.model_copy(update={"message_ids": [message.id]})
                reply = self.reply.model_copy(deep=True)
                draft = reply.proposed_statements[0]
                draft.source = "explicit"
                draft.document_evidence = []
                draft.evidence = [Evidence(message_id=message.id, quote=self.evidence.quote)]
                if role == "user" and text == self.evidence.quote:
                    _, staged = bench.propose(revision, selection, reply, "synthetic")
                    self.assertEqual(staged.proposals[0].reply, reply)
                    self.assertEqual(staged.proposals[0].repaired_document_statement_indices, [])
                else:
                    before = bench.store.load()
                    with self.assertRaisesRegex(UserError, "Map note 1, message citation 1"):
                        bench.propose(revision, selection, reply, "synthetic")
                    self.assertEqual(bench.store.load(), before)

    def test_reclassification_respects_document_citation_limit(self):
        reply = self.reply.model_copy(deep=True)
        draft = reply.proposed_statements[0]
        draft.document_evidence = [self.evidence.model_copy(update={"quote": self.evidence.quote[:i]}) for i in range(1, 9)]
        draft.evidence = [Evidence(message_id=self.document.id, quote="Built a tool rack.")]
        before = self.store.load()
        with self.assertRaisesRegex(UserError, "limit of eight document citations"):
            self.bench.propose(self.revision, self.selection, reply, "synthetic")
        self.assertEqual(self.store.load(), before)

    def test_invalid_document_evidence_and_source_labels_are_atomic(self):
        cases = [
            self.reply.proposed_statements[0].model_copy(update={"source": "explicit"}),
            self.reply.proposed_statements[0].model_copy(update={"document_evidence": []}),
            self.reply.proposed_statements[0].model_copy(update={"flexibility": "hard_limit"}),
            self.reply.proposed_statements[0].model_copy(update={"document_evidence": [self.evidence.model_copy(update={"quote": "Invented credential"})]}),
            self.reply.proposed_statements[0].model_copy(update={"document_evidence": [self.evidence.model_copy(update={"start_line": 1, "end_line": 1, "quote": OMITTED})]}),
        ]
        before = self.store.load()
        for draft in cases:
            with self.subTest(source=draft.source, evidence=draft.document_evidence), self.assertRaises(UserError):
                self.bench.propose(self.revision, self.selection, ModelReply(assistant_text="Draft", proposed_statements=[draft]), "synthetic")
            self.assertEqual(self.store.load(), before)

    def test_prompt_numbers_selected_lines_without_duplicating_or_expanding_source_text(self):
        context = select_context(self.workspace, self.selection)
        prompt = build_prompt(context)
        selected = json.loads(prompt.split("BEGIN_JOBWOLF_CONTEXT_JSON\n", 1)[1].split("\n", 1)[0])
        document = selected["documents"][0]
        self.assertNotIn("text", document)
        self.assertEqual(document["lines"], [
            {"line": 2, "text": ""},
            {"line": 3, "text": "Repaired bicycles at a community workshop."},
            {"line": 4, "text": "Built a tool rack."},
        ])
        self.assertNotIn(OMITTED, prompt)
        self.assertNotIn("Unselected final line", prompt)
        self.assertEqual(structured_input_size(context).input_bytes, len(prompt.encode()))

    def test_unique_quote_repair_is_flagged_reviewable_and_preserves_provenance(self):
        reply = self.reply.model_copy(deep=True)
        reply.proposed_statements[0].document_evidence[0].start_line = 4
        reply.proposed_statements[0].document_evidence[0].end_line = 4
        before = reply.model_dump()
        with self.assertRaises(UserError):
            validate_reply(reply, select_context(self.workspace, self.selection))
        revision, staged = self.bench.propose(self.revision, self.selection, reply, "synthetic")
        proposal = staged.proposals[0]
        self.assertEqual(proposal.repaired_document_statement_indices, [0])
        self.assertEqual(proposal.reply.proposed_statements[0].document_evidence, [self.evidence])
        self.assertEqual(reply.model_dump(), before)
        self.assertEqual(staged.documents[0], self.document)
        self.assertFalse(staged.statements)
        restored = Workspace.model_validate_json(staged.model_dump_json())
        revision, restored = self.bench.replace(revision, restored)
        self.assertEqual(restored.proposals[0].repaired_document_statement_indices, [0])
        _, accepted = self.bench.accept(proposal.id, AcceptRequest(revision=revision, statement_indices=[0]))
        self.assertEqual(accepted.statements[0].source, "document")
        self.assertEqual(accepted.statements[0].document_evidence, [self.evidence])

    def test_whitespace_repair_restores_original_quote_and_multiline_locator(self):
        revision, workspace = self.bench.add_document(DocumentSaveRequest(
            revision=self.revision, filename="Synthetic wrapping.txt", format="txt",
            text="Heading\n  Maintained\tand  repaired\nproduction equipment.\nEnd",
        ))
        document = workspace.documents[-1]
        selection = ContextSelection(document_ranges=[DocumentSelection(document_id=document.id, start_line=2, end_line=3)])
        reply = self.reply.model_copy(deep=True)
        reply.proposed_statements[0].document_evidence = [DocumentEvidence(
            document_id=document.id, start_line=2, end_line=2,
            quote="Maintained and repaired production equipment.",
        )]
        _, staged = self.bench.propose(revision, selection, reply, "synthetic")
        proposal = staged.proposals[0]
        actual = proposal.reply.proposed_statements[0].document_evidence[0]
        self.assertEqual((actual.start_line, actual.end_line), (2, 3))
        self.assertEqual(actual.quote, "Maintained\tand  repaired\nproduction equipment.")
        self.assertEqual(proposal.repaired_document_statement_indices, [0])
        validate_reply(proposal.reply, select_context(staged, selection))

    def test_valid_citations_are_not_rewritten_or_flagged(self):
        _, staged = self.bench.propose(self.revision, self.selection, self.reply, "synthetic")
        self.assertEqual(staged.proposals[0].reply, self.reply)
        self.assertEqual(staged.proposals[0].repaired_document_statement_indices, [])

    def test_repair_never_uses_another_document_or_an_unselected_passage(self):
        revision, workspace = self.bench.add_document(DocumentSaveRequest(
            revision=self.revision, filename="Another synthetic.txt", format="txt", text="Only in another source.",
        ))
        other = workspace.documents[-1]
        selection = ContextSelection(document_ranges=[self.range, DocumentSelection(document_id=other.id, start_line=1, end_line=1)])
        before = self.store.load()
        for quote in (OMITTED, "Only in another source."):
            reply = self.reply.model_copy(deep=True)
            reply.proposed_statements[0].document_evidence[0].quote = quote
            with self.subTest(quote=quote), self.assertRaisesRegex(UserError, "not found"):
                self.bench.propose(revision, selection, reply, "synthetic")
            self.assertEqual(self.store.load(), before)
        reply = self.reply.model_copy(deep=True)
        reply.proposed_statements[0].document_evidence = [DocumentEvidence(document_id=other.id, start_line=1, end_line=1, quote=other.text)]
        with self.assertRaisesRegex(UserError, "document was not selected"):
            self.bench.propose(revision, self.selection, reply, "synthetic")
        self.assertEqual(self.store.load(), before)

    def test_ambiguous_exact_whitespace_and_overlapping_matches_are_not_guessed(self):
        for text, quote in (
            ("Heading\nRepeated quote\nRepeated quote", "Repeated quote"),
            ("Heading\nRepeated\tquote\nRepeated  quote", "Repeated quote"),
            ("Heading\nbanana\nEnd", "ana"),
        ):
            with self.subTest(quote=quote):
                store = MemoryStore()
                bench = Bench(store)
                revision, workspace = bench.add_document(DocumentSaveRequest(revision=0, filename="Synthetic.txt", format="txt", text=text))
                document = workspace.documents[0]
                selection = ContextSelection(document_ranges=[DocumentSelection(document_id=document.id, start_line=1, end_line=3)])
                reply = self.reply.model_copy(deep=True)
                reply.proposed_statements[0].document_evidence = [DocumentEvidence(document_id=document.id, start_line=1, end_line=1, quote=quote)]
                before = store.load()
                with self.assertRaisesRegex(UserError, "more than one location"):
                    bench.propose(revision, selection, reply, "synthetic")
                self.assertEqual(store.load(), before)

    def test_paraphrased_or_changed_quotes_fail_atomically_with_safe_note_positions(self):
        before = self.store.load()
        for quote in (
            "Fixed bicycles at a community workshop.",
            "Repaired bicycles at a community workshop!",
            "repaired bicycles at a community workshop.",
            "Repaired no bicycles at a community workshop.",
        ):
            reply = self.reply.model_copy(deep=True)
            bad = reply.proposed_statements[0].model_copy(deep=True)
            bad.document_evidence[0].quote = quote
            reply.proposed_statements.append(bad)
            with self.subTest(quote=quote), self.assertRaises(UserError) as error:
                self.bench.propose(self.revision, self.selection, reply, "synthetic")
            self.assertIn("Map note 2, document citation 1", str(error.exception))
            self.assertIn("Nothing was changed", str(error.exception))
            self.assertNotIn(quote, str(error.exception))
            self.assertNotIn(self.document.filename, str(error.exception))
            self.assertEqual(self.store.load(), before)

    def test_repair_metadata_is_application_owned_and_old_proposals_still_load(self):
        _, staged = self.bench.propose(self.revision, self.selection, self.reply, "synthetic")
        old = staged.model_dump()
        del old["proposals"][0]["repaired_document_statement_indices"]
        self.assertEqual(Workspace.model_validate(old), staged)
        for indices in ([1], [0, 0], [-1]):
            data = staged.model_dump()
            data["proposals"][0]["repaired_document_statement_indices"] = indices
            with self.subTest(indices=indices), self.assertRaises(ValidationError):
                Workspace.model_validate(data)
        with self.assertRaises(ValidationError):
            ModelReply.model_validate({**self.reply.model_dump(), "repaired_document_statement_indices": [0]})

    def test_new_explicit_correction_does_not_rewrite_document_snapshot(self):
        revision, workspace = self.bench.add_message(self.revision, "That résumé is outdated; I no longer want repair work.")
        message = workspace.messages[-1]
        draft = StatementDraft(text="No longer wants repair work", domain="work", source="explicit", evidence=[Evidence(message_id=message.id, quote=message.text)])
        selection = self.selection.model_copy(update={"message_ids": [message.id]})
        revision, staged = self.bench.propose(revision, selection, ModelReply(assistant_text="A correction", proposed_statements=[draft]), "synthetic")
        _, accepted = self.bench.accept(staged.proposals[0].id, AcceptRequest(revision=revision, statement_indices=[0]))
        self.assertEqual(accepted.documents[0], self.document)
        self.assertEqual(accepted.statements[0].source, "explicit")

    def test_notes_and_messages_do_not_implicitly_disclose_source_text(self):
        statement = Statement(**self.reply.proposed_statements[0].model_dump(), confirmed=True)
        message = Message(role="user", text="Let's talk", document_ranges=[self.range])
        self.workspace.statements.append(statement)
        self.workspace.messages.append(message)
        context = select_context(self.workspace, ContextSelection(message_ids=[message.id], statement_ids=[statement.id]))
        self.assertEqual(context.documents, [])
        self.assertEqual(context.statements[0].document_evidence, [])
        self.assertEqual(context.messages[0].document_ranges, [])
        self.assertEqual(context.statements[0].source, "document")
        schema = _reply_schema(context)
        self.assertNotIn("DocumentEvidence", schema["$defs"])
        self.assertNotIn("document_evidence", schema["$defs"]["StatementDraft"]["properties"])
        self.assertNotIn(OMITTED, build_prompt(context))

    def test_chat_attachments_are_explicit_one_turn_and_retryable(self):
        revision, workspace = self.bench.add_message(self.revision, "Use these excerpts", [self.range])
        message_id = workspace.messages[-1].id
        selection, context = select_chat_context(workspace, message_id)
        self.assertEqual(selection.document_ranges, [self.range])
        self.assertEqual(select_chat_context(workspace, message_id), (selection, context))
        prompt = json.dumps(chat_messages(context))
        self.assertNotIn(OMITTED, prompt)
        self.assertIn("untrusted document claims", prompt)
        with self.assertRaisesRegex(UserError, "fewer document lines"):
            select_chat_context(workspace, message_id, input_budget=chat_input_bytes(context) - 1)
        revision, workspace = self.bench.answer_chat(revision, message_id, "What did you enjoy?")
        _, workspace = self.bench.add_message(revision, "Let's discuss something else")
        _, ordinary = select_chat_context(workspace, workspace.messages[-1].id)
        self.assertEqual(ordinary.documents, [])
        self.assertNotIn("Repaired bicycles", json.dumps(chat_messages(ordinary)))

    def test_ranges_names_and_tampered_hashes_fail_validation(self):
        for ranges in [[self.range, self.range], [self.range, DocumentSelection(document_id=self.document.id, start_line=4, end_line=5)]]:
            with self.assertRaises(ValidationError):
                ContextSelection(document_ranges=ranges)
        with self.assertRaises(UserError):
            select_context(self.workspace, ContextSelection(document_ranges=[DocumentSelection(document_id=self.document.id, start_line=1, end_line=100)]))
        for filename in ["../secret.txt", r"a\b.txt", "C:resume", "bad\ud800"]:
            with self.subTest(filename=repr(filename)), self.assertRaises(ValidationError):
                DocumentSaveRequest(revision=1, filename=filename, format="txt", text="Synthetic")
        altered = self.workspace.model_dump()
        altered["documents"][0]["text"] = "Tampered"
        with self.assertRaises(ValidationError):
            Workspace.model_validate(altered)

    def test_duplicate_text_rejected_and_corrections_create_new_snapshots(self):
        with self.assertRaisesRegex(UserError, "already saved"):
            self.bench.add_document(DocumentSaveRequest(revision=self.revision, filename="Other name", format="paste", text=TEXT))
        _, updated = self.bench.add_document(DocumentSaveRequest(revision=self.revision, filename=self.document.filename, format="txt", text="Corrected synthetic text"))
        self.assertEqual(updated.documents[0], self.document)
        self.assertNotEqual(updated.documents[1].id, self.document.id)
        self.assertNotEqual(updated.documents[1].text_sha256, self.document.text_sha256)

    def test_deletion_removes_citations_derivatives_and_attachments_but_not_conversation(self):
        revision, workspace = self.bench.add_message(self.revision, "Conversation repeats a document claim", [self.range])
        revision, workspace = self.bench.propose(revision, self.selection, self.reply, "synthetic")
        revision, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, statement_indices=[0]))
        note = workspace.statements[0]
        workspace.statements.append(Statement(text="Keep this uncited note", domain="life", related_statement_ids=[note.id]))
        workspace.opportunities.append(Opportunity(title="Synthetic path", kind="career", rationale="A hypothesis", origin="test", based_on_map_version=workspace.map_version, statement_ids=[note.id]))
        workspace.brief = Brief(title="Synthetic brief", summary="A plan", origin="test", based_on_map_version=workspace.map_version)
        revision, workspace = self.bench.replace(revision, workspace)
        revision, workspace = self.bench.propose(revision, self.selection, self.reply, "synthetic")
        impact = self.bench.document_impact(self.document.id)
        self.assertEqual(impact, {"revision": revision, "statement_ids": [note.id], "proposal_count": 1, "opportunity_count": 1, "has_brief": True, "message_count": 1})
        with self.assertRaises(ConflictError):
            self.bench.delete_document(revision - 1, self.document.id)
        _, deleted = self.bench.delete_document(revision, self.document.id)
        self.assertEqual(deleted.documents, [])
        self.assertEqual(len(deleted.statements), 1)
        self.assertEqual(deleted.statements[0].related_statement_ids, [])
        self.assertEqual(deleted.messages[0].text, "Conversation repeats a document claim")
        self.assertTrue(all(not m.document_ranges for m in deleted.messages))
        self.assertFalse(deleted.proposals or deleted.opportunities or deleted.brief)
        self.assertNotIn(self.document.id, deleted.model_dump_json())
        Workspace.model_validate_json(deleted.model_dump_json())

    def test_v1_migration_is_read_only_until_next_revisioned_write(self):
        message = Message(role="user", text="Existing synthetic conversation")
        data = Workspace(messages=[message], statements=[Statement(text="Existing note", domain="work")]).model_dump(mode="json")
        data["schema_version"] = 1
        data.pop("documents")
        data["messages"][0].pop("document_ranges")
        data["statements"][0].pop("document_evidence")
        store = MemoryStore()
        store.payload = json.dumps(data).encode()
        original = store.payload
        bench = Bench(store)
        revision, migrated = bench.load()
        self.assertEqual(store.payload, original)
        self.assertEqual(revision, 0)
        self.assertEqual(migrated.schema_version, 2)
        self.assertEqual(migrated.messages[0].id, message.id)
        self.assertEqual(migrated.statements[0].id, data["statements"][0]["id"])
        revision, saved = bench.add_message(revision, "A new turn")
        self.assertEqual(revision, 1)
        self.assertEqual(json.loads(store.payload)["schema_version"], 2)
        self.assertEqual(saved.messages[0].id, message.id)


class DocumentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.provider = SyntheticProvider()
        for name in ("socket.create_connection", "socket.getaddrinfo"):
            guard = patch(name, side_effect=AssertionError("No real network in résumé tests"))
            guard.start()
            self.addCleanup(guard.stop)
        self.app = create_app(store=self.store, provider=self.provider, token=TOKEN)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765", headers={"X-JobWolf-Token": TOKEN})
        self.addCleanup(self.client.close)

    def test_preview_is_read_only_provider_free_and_does_not_retain_original(self):
        payload = {"filename": "synthetic.txt", "content_base64": encoded(TEXT.encode())}
        with patch.object(self.provider, "list_models", side_effect=AssertionError("Preview must not discover models")):
            response = self.client.post("/api/documents/preview", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.store.payload)
        self.assertEqual(self.store.revision, 0)
        self.assertEqual(self.provider.contexts, [])
        saved = self.client.post("/api/documents", json={"revision": 0, "filename": response.json()["filename"], "format": "txt", "text": "Approved excerpt only"})
        self.assertEqual(saved.status_code, 200, saved.text)
        exported = self.client.get("/api/export")
        self.assertNotIn(OMITTED, exported.text)
        self.assertNotIn("content_base64", exported.text)
        self.assertEqual(exported.json()["documents"][0]["text"], "Approved excerpt only")
        self.assertEqual(exported.json()["messages"], [])

    def test_preview_auth_type_limits_and_safe_errors(self):
        payload = {"filename": "synthetic.txt", "content_base64": encoded(b"Synthetic")}
        self.assertEqual(self.client.post("/api/documents/preview", json=payload, headers={"X-JobWolf-Token": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/documents/preview", content="bad", headers={"Content-Type": "text/plain"}).status_code, 415)
        self.assertEqual(self.client.post("/api/documents/preview", json=payload, headers={"Origin": "https://elsewhere.invalid"}).status_code, 403)
        response = self.client.post("/api/documents/preview", json={"filename": "synthetic.doc", "content_base64": encoded(b"SYNTHETIC_PRIVATE_MARKER")})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["state_changed"])
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", response.text)
        response = self.client.post("/api/documents/preview", json={"filename": "synthetic.txt", "content_base64": "A" * 2796205})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.revision, 0)

    def test_source_only_manual_packet_citation_review_export_and_delete(self):
        response = self.client.post("/api/documents", json={"revision": 0, "filename": "Synthetic.txt", "format": "txt", "text": TEXT})
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        identifier = state["workspace"]["documents"][0]["id"]
        selection = {"task": "conversation", "message_ids": [], "statement_ids": [], "document_ranges": [{"document_id": identifier, "start_line": 3, "end_line": 3}]}
        packet = self.client.post("/api/context", json={"revision": 1, "selection": selection})
        self.assertEqual(packet.status_code, 200, packet.text)
        self.assertEqual(packet.json()["selection"], selection)
        self.assertNotIn(OMITTED, packet.text)
        reply = {"assistant_text": "A tentative starting point", "proposed_statements": [{"text": "Résumé describes repair experience", "domain": "background", "source": "document", "document_evidence": [{**selection["document_ranges"][0], "start_line": 2, "end_line": 2, "quote": "Repaired bicycles at a community workshop."}]}]}
        staged = self.client.post("/api/proposals/manual", json={"revision": 1, "selection": selection, "reply_text": json.dumps(reply)})
        self.assertEqual(staged.status_code, 200, staged.text)
        proposal = staged.json()["workspace"]["proposals"][0]
        self.assertEqual(proposal["repaired_document_statement_indices"], [0])
        self.assertEqual(proposal["reply"]["proposed_statements"][0]["document_evidence"][0]["start_line"], 3)
        accepted = self.client.post(f"/api/proposals/{proposal['id']}/accept", json={"revision": 2, "statement_indices": [0]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        exported = self.client.get("/api/export").json()
        imported = self.client.post("/api/import", json={"revision": 3, "confirmation": "REPLACE", "workspace": exported})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["workspace"], exported)
        self.assertEqual(self.client.request("DELETE", f"/api/documents/{identifier}", json={"revision": 4}).status_code, 422)
        removed = self.client.request("DELETE", f"/api/documents/{identifier}", json={"revision": 4, "confirmation": "DELETE"})
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertFalse(removed.json()["workspace"]["documents"])
        self.assertFalse(removed.json()["workspace"]["statements"])
        self.assertEqual(self.provider.contexts, [])

    def test_resume_only_generate_recovers_document_quote_in_message_field(self):
        saved = self.client.post("/api/documents", json={"revision": 0, "filename": "Synthetic.txt", "format": "txt", "text": TEXT})
        self.assertEqual(saved.status_code, 200, saved.text)
        identifier = saved.json()["workspace"]["documents"][0]["id"]
        selection = {"document_ranges": [{"document_id": identifier, "start_line": 3, "end_line": 3}]}
        reply = ModelReply(assistant_text="A starting point for review", proposed_statements=[StatementDraft(
            text="The résumé describes repair experience", domain="background", source="explicit",
            evidence=[Evidence(message_id=identifier, quote="Repaired bicycles at a community workshop.")],
        )])
        with patch.object(self.provider, "generate", return_value=reply) as generate:
            response = self.client.post("/api/generate", json={"revision": 1, "selection": selection, "model": "Lyra:latest"})
        self.assertEqual(response.status_code, 200, response.text)
        generate.assert_called_once()
        context = generate.call_args.args[1]
        self.assertEqual(context.messages, [])
        self.assertNotIn(OMITTED, context.model_dump_json())
        workspace = response.json()["workspace"]
        self.assertEqual(workspace["statements"], [])
        self.assertTrue(all(message["role"] == "assistant" for message in workspace["messages"]))
        proposal = workspace["proposals"][0]
        self.assertEqual(proposal["repaired_document_statement_indices"], [0])
        draft = proposal["reply"]["proposed_statements"][0]
        self.assertEqual(draft["source"], "document")
        self.assertEqual(draft["evidence"], [])
        self.assertEqual(draft["document_evidence"], [{**selection["document_ranges"][0], "quote": "Repaired bicycles at a community workshop."}])

    def test_http_chat_sends_only_attached_excerpt_and_never_adds_map_notes(self):
        saved = self.client.post("/api/documents", json={"revision": 0, "filename": "Synthetic.txt", "format": "txt", "text": TEXT}).json()
        identifier = saved["workspace"]["documents"][0]["id"]
        message = self.client.post("/api/messages", json={"revision": 1, "text": "Use this starting point", "document_ranges": [{"document_id": identifier, "start_line": 3, "end_line": 3}]}).json()
        response = self.client.post("/api/chat", json={"revision": 2, "model": "Lyra:latest", "message_id": message["workspace"]["messages"][-1]["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(len(events[0]["selection"]["document_ranges"]), 1)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["state"]["workspace"]["statements"], [])
        self.assertNotIn(OMITTED, self.provider.contexts[0].model_dump_json())
        self.assertEqual(self.provider.contexts[0].documents[0].text, "Repaired bicycles at a community workshop.")

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI required")
    def test_approved_source_is_encrypted_in_temporary_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.sqlite3"
            bench = Bench(SQLiteStore(path))
            _, workspace = bench.add_document(DocumentSaveRequest(revision=0, filename="Synthetic.txt", format="txt", text=TEXT))
            self.assertEqual(workspace.documents[0].text_sha256, hashlib.sha256(TEXT.encode()).hexdigest())
            self.assertNotIn(OMITTED.encode(), path.read_bytes())
            self.assertEqual(Bench(SQLiteStore(path)).load()[1].documents, workspace.documents)


if __name__ == "__main__":
    unittest.main()
