"""Synthetic domain tests; no real profiles, files, providers, or network."""

import unittest
from typing import Any, Literal, cast

from pydantic import ValidationError

from jobwolf.models import (
    AcceptRequest,
    BriefDraft,
    ContextSelection,
    Evidence,
    Message,
    ModelReply,
    OpportunityDraft,
    Statement,
    StatementDraft,
    Workspace,
)
from jobwolf.service import Bench, MissingRecord, UserError, render_brief, select_context, validate_reply
from jobwolf.storage import ConflictError, StorageError


class MemoryStore:
    """Test-only store. Runtime configuration never offers plaintext storage."""
    def __init__(self):
        self.revision = 0
        self.payload: bytes | None = None

    def load(self):
        return self.revision, self.payload

    def save(self, payload, expected_revision):
        if expected_revision != self.revision:
            raise ConflictError("Stale test revision")
        self.revision += 1
        self.payload = payload
        return self.revision


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.bench = Bench(self.store)
        self.revision, self.workspace = self.bench.add_message(0, "I like fixing bicycles. I could accept lower pay for a four-day week.")
        self.message = self.workspace.messages[0]
        self.selection = ContextSelection(message_ids=[self.message.id])

    def reply(self, source: Literal["explicit", "inferred"] = "explicit"):
        return ModelReply(
            assistant_text="You seem to enjoy practical problem-solving. What conditions matter most?",
            proposed_statements=[StatementDraft(
                text="Enjoys fixing bicycles", domain="work", source=source,
                evidence=[Evidence(message_id=self.message.id, quote="I like fixing bicycles.")],
            )],
        )

    def stage(self, reply=None):
        return self.bench.propose(self.revision, self.selection, reply or self.reply(), "synthetic provider")

    def test_proposal_never_mutates_map_before_acceptance(self):
        revision, workspace = self.stage()
        self.assertEqual(workspace.statements, [])
        self.assertEqual(workspace.map_version, 0)
        self.assertEqual(len(workspace.messages), 2)
        self.assertEqual(len(workspace.proposals), 1)
        self.assertGreater(revision, self.revision)

    def test_acceptance_preserves_inference_and_evidence(self):
        revision, workspace = self.stage(self.reply("inferred"))
        revision, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, statement_indices=[0]))
        statement = workspace.statements[0]
        self.assertEqual(statement.source, "inferred")
        self.assertTrue(statement.confirmed)
        self.assertEqual(statement.evidence[0].message_id, self.message.id)
        self.assertEqual(workspace.proposals, [])
        self.assertEqual(workspace.map_version, 1)

    def test_five_message_response_with_bad_optional_links_can_be_reviewed(self):
        revision, workspace = self.revision, self.workspace
        for index in range(4):
            revision, workspace = self.bench.add_message(revision, f"Synthetic follow-up {index}")
        selection = ContextSelection(message_ids=[m.id for m in workspace.messages])
        reply = self.reply()
        reply.opportunities.append(OpportunityDraft(
            title="Explore practical repair", kind="career", rationale="A hypothesis to investigate",
            statement_ids=[self.message.id, "ffffffff-ffff-ffff-ffff-ffffffffffff"],
            unknowns=[f"Unknown {index}" for index in range(8)],
        ))
        original = reply.model_dump()
        revision, workspace = self.bench.propose(revision, selection, reply, "synthetic provider")
        proposal = workspace.proposals[0]
        self.assertEqual(len(workspace.messages), 6)
        self.assertEqual(workspace.messages[-1].text, reply.assistant_text)
        self.assertEqual(workspace.statements, [])
        self.assertEqual(proposal.reply.opportunities[0].statement_ids, [])
        self.assertEqual(proposal.unverified_opportunity_indices, [0])
        self.assertEqual(proposal.reply.opportunities[0].unknowns, reply.opportunities[0].unknowns)
        self.assertEqual(reply.model_dump(), original)
        _, workspace = self.bench.accept(proposal.id, AcceptRequest(
            revision=revision, statement_indices=[0], opportunity_indices=[0],
        ))
        self.assertEqual(workspace.opportunities[0].statement_ids, [])
        self.assertTrue(workspace.opportunities[0].unverified_links)
        self.assertEqual(workspace.opportunities[0].status, "hypothesis")
        self.assertTrue(workspace.statements[0].confirmed)

    def test_recovery_keeps_only_selected_links_and_warning_survives_exports(self):
        revision, workspace = self.bench.save_statement(self.revision, StatementDraft(text="Selected preference", domain="work"))
        selected_id = workspace.statements[0].id
        revision, workspace = self.bench.save_statement(revision, StatementDraft(text="Unselected private preference", domain="life"))
        unselected_id = workspace.statements[1].id
        selection = ContextSelection(task="discover", message_ids=[self.message.id], statement_ids=[selected_id])
        reply = ModelReply(
            assistant_text="Two exploratory directions",
            opportunities=[
                OpportunityDraft(title="Mixed links", kind="career", rationale="A hypothesis", statement_ids=[unselected_id, selected_id, self.message.id]),
                OpportunityDraft(title="Selected links", kind="career", rationale="Another hypothesis", statement_ids=[selected_id]),
            ],
            brief=BriefDraft(title="Plan", summary="Two hypotheses to investigate"),
        )
        with self.assertRaisesRegex(UserError, "outside the selected context"):
            validate_reply(reply, self.bench.context(revision, selection))
        revision, workspace = self.bench.propose(revision, selection, reply, "test")
        proposal = workspace.proposals[0]
        self.assertEqual(proposal.unverified_opportunity_indices, [0])
        self.assertEqual(proposal.reply.opportunities[0].statement_ids, [selected_id])
        self.assertEqual(proposal.selection, selection)
        _, workspace = self.bench.accept(proposal.id, AcceptRequest(revision=revision, opportunity_indices=[1, 0], include_brief=True))
        self.assertFalse(workspace.opportunities[0].unverified_links)
        self.assertTrue(workspace.opportunities[1].unverified_links)
        restored = Workspace.model_validate_json(workspace.model_dump_json())
        self.assertTrue(restored.opportunities[1].unverified_links)
        self.assertIn("unverified Person Map links were omitted", render_brief(restored))
        self.assertEqual(restored.statements[1].text, "Unselected private preference")

    def test_optional_link_recovery_does_not_relax_personal_evidence_validation(self):
        for violation in ("quote", "relationship", "hard_limit"):
            reply = self.reply()
            reply.opportunities.append(OpportunityDraft(title="A hypothesis", kind="career", rationale="A rationale", statement_ids=[self.message.id]))
            if violation == "quote":
                reply.proposed_statements[0].evidence[0].quote = "Invented quotation"
            elif violation == "relationship":
                reply.proposed_statements[0].related_statement_ids = [self.message.id]
            else:
                reply.proposed_statements[0].source = "inferred"
                reply.proposed_statements[0].flexibility = "hard_limit"
            with self.subTest(violation=violation), self.assertRaises(UserError):
                self.stage(reply)
            self.assertEqual(self.store.revision, self.revision)

    def test_warning_metadata_is_validated_and_old_workspaces_remain_readable(self):
        reply = ModelReply(assistant_text="A reflection", opportunities=[OpportunityDraft(title="A possibility", kind="career", rationale="A hypothesis")])
        revision, workspace = self.stage(reply)
        self.assertEqual(workspace.proposals[0].unverified_opportunity_indices, [])
        for indices in ([1], [0, 0], [-1]):
            data = workspace.model_dump(mode="json")
            data["proposals"][0]["unverified_opportunity_indices"] = indices
            with self.subTest(indices=indices), self.assertRaises(ValidationError):
                Workspace.model_validate(data)
        old_proposal_data = workspace.model_dump(mode="json")
        del old_proposal_data["proposals"][0]["unverified_opportunity_indices"]
        self.assertEqual(Workspace.model_validate(old_proposal_data), workspace)
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, opportunity_indices=[0]))
        old_saved_data = workspace.model_dump(mode="json")
        del old_saved_data["opportunities"][0]["unverified_links"]
        self.assertEqual(Workspace.model_validate(old_saved_data), workspace)
        with self.assertRaises(ValidationError):
            ModelReply.model_validate({"assistant_text": "Hi", "unverified_opportunity_indices": [0]})

    def test_fabricated_or_unselected_evidence_rejected_atomically(self):
        for mutation in ("quote", "id", "explicit_without_evidence", "inferred_hard_limit"):
            with self.subTest(mutation=mutation):
                reply = self.reply()
                if mutation == "quote":
                    reply.proposed_statements[0].evidence[0].quote = "This was never said"
                elif mutation == "id":
                    reply.proposed_statements[0].evidence[0].message_id = Message(role="user", text="other").id
                elif mutation == "explicit_without_evidence":
                    reply.proposed_statements[0].evidence = []
                else:
                    reply.proposed_statements[0].source = "inferred"
                    reply.proposed_statements[0].flexibility = "hard_limit"
                with self.assertRaises(UserError):
                    self.stage(reply)
                self.assertEqual(self.store.revision, self.revision)
                self.assertEqual(self.bench.load()[1].proposals, [])

    def test_assistant_text_is_not_user_evidence(self):
        assistant = Message(role="assistant", text="Invented personal fact")
        workspace = Workspace(messages=[assistant])
        self.bench.replace(self.revision, workspace)
        reply = ModelReply(assistant_text="Hello", proposed_statements=[StatementDraft(
            text=assistant.text, domain="work", source="explicit",
            evidence=[Evidence(message_id=assistant.id, quote=assistant.text)],
        )])
        with self.assertRaises(UserError):
            self.bench.propose(self.store.revision, ContextSelection(message_ids=[assistant.id]), reply, "model")

    def test_unselected_quotes_and_relationships_do_not_leave_context(self):
        secret = Message(role="user", text="SYNTHETIC SECRET MESSAGE")
        other = Statement(text="SYNTHETIC SECRET STATEMENT", domain="money")
        selected = Statement(text="Selected preference", domain="work", related_statement_ids=[other.id], evidence=[Evidence(message_id=secret.id, quote=secret.text)])
        workspace = Workspace(messages=[secret], statements=[selected, other])
        context = select_context(workspace, ContextSelection(statement_ids=[selected.id]))
        self.assertNotIn("SYNTHETIC SECRET", context.model_dump_json())
        self.assertEqual(context.statements[0].evidence, [])
        self.assertEqual(context.statements[0].related_statement_ids, [])
        self.assertEqual(workspace.statements[0].evidence[0].quote, secret.text)

    def test_stale_reply_cannot_overwrite_newer_user_message(self):
        self.bench.add_message(self.revision, "Newer information")
        with self.assertRaises(ConflictError):
            self.stage()
        self.assertEqual(len(self.bench.load()[1].messages), 2)

    def test_map_edit_invalidates_existing_proposal(self):
        revision, workspace = self.stage()
        proposal = workspace.proposals[0]
        revision, _ = self.bench.save_statement(revision, StatementDraft(text="New preference", domain="life"))
        with self.assertRaises(ConflictError):
            self.bench.accept(proposal.id, AcceptRequest(revision=revision, statement_indices=[0]))

    def test_accept_indices_must_be_valid_unique_and_nonempty(self):
        revision, workspace = self.stage()
        for indices in ([1], [0, 0], []):
            with self.subTest(indices=indices), self.assertRaises(UserError):
                self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, statement_indices=indices))
        for indices in ([-1], [True], ["0"]):
            with self.subTest(indices=indices), self.assertRaises(ValidationError):
                AcceptRequest(revision=revision, statement_indices=cast(Any, indices))
        self.assertEqual(self.store.revision, revision)

    def test_rejection_does_not_change_map_or_erase_conversation(self):
        revision, workspace = self.stage()
        _, result = self.bench.reject(revision, workspace.proposals[0].id)
        self.assertFalse(result.proposals)
        self.assertFalse(result.statements)
        self.assertEqual(len(result.messages), 2)

    def test_hard_gates_are_compiled_from_reviewed_map_not_model(self):
        revision, _ = self.bench.save_statement(self.revision, StatementDraft(text="No overnight travel", domain="life", flexibility="hard_limit", source="explicit"))
        reply = ModelReply(assistant_text="A provisional plan", brief=BriefDraft(title="Plan", summary="Explore options", hard_gates=["Invented minimum salary"]))
        revision, workspace = self.bench.propose(revision, self.selection, reply, "synthetic")
        proposed_brief = workspace.proposals[0].reply.brief
        assert proposed_brief is not None
        self.assertEqual(proposed_brief.hard_gates, ["No overnight travel"])
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        assert workspace.brief is not None
        self.assertEqual(workspace.brief.hard_gates, ["No overnight travel"])

    def test_more_than_twelve_real_hard_limits_are_not_silently_dropped(self):
        workspace = Workspace(statements=[Statement(text=f"Constraint {i}", domain="life", flexibility="hard_limit", confirmed=True) for i in range(13)])
        revision, workspace = self.bench.replace(self.revision, workspace)
        selection = ContextSelection(statement_ids=[workspace.statements[0].id])
        reply = ModelReply(assistant_text="A plan", brief=BriefDraft(title="Plan", summary="A summary"))
        revision, workspace = self.bench.propose(revision, selection, reply, "test")
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        assert workspace.brief is not None
        self.assertEqual(len(workspace.brief.hard_gates), 13)

    def test_feedback_is_evidence_not_an_automatic_preference(self):
        reply = ModelReply(assistant_text="Try this", opportunities=[OpportunityDraft(title="Repair work", kind="career", rationale="A hypothesis")])
        revision, workspace = self.stage(reply)
        revision, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, opportunity_indices=[0]))
        _, workspace = self.bench.feedback(revision, workspace.opportunities[0].id, "unsure", "Concerned about repetition")
        self.assertEqual(workspace.map_version, 0)
        self.assertFalse(workspace.statements)
        self.assertIn("Concerned about repetition", workspace.messages[-1].text)
        self.assertEqual(workspace.messages[-1].role, "user")

    def test_deleting_statement_clears_derived_data_and_links(self):
        revision, workspace = self.bench.save_statement(self.revision, StatementDraft(text="Preference", domain="work"))
        statement_id = workspace.statements[0].id
        revision, workspace = self.bench.save_statement(revision, StatementDraft(text="Related", domain="life", related_statement_ids=[statement_id]))
        reply = ModelReply(assistant_text="Summary", brief=BriefDraft(title="Plan", summary="Preference-derived text"))
        revision, workspace = self.bench.propose(revision, self.selection, reply, "test")
        revision, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        _, workspace = self.bench.delete_statement(revision, statement_id)
        self.assertEqual(workspace.statements[0].related_statement_ids, [])
        self.assertIsNone(workspace.brief)
        self.assertFalse(workspace.proposals)
        self.assertTrue(workspace.messages)

    def test_edit_marks_brief_stale_and_preserves_statement_identity(self):
        revision, workspace = self.bench.save_statement(self.revision, StatementDraft(text="Old", domain="work"))
        identifier = workspace.statements[0].id
        reply = ModelReply(assistant_text="Summary", brief=BriefDraft(title="Plan", summary="First plan"))
        revision, workspace = self.bench.propose(revision, self.selection, reply, "test")
        revision, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        _, workspace = self.bench.save_statement(revision, StatementDraft(text="Corrected", domain="work"), identifier)
        self.assertEqual(workspace.statements[0].id, identifier)
        self.assertIn("user-edited", workspace.statements[0].origin)
        self.assertIn("STALE", render_brief(workspace))

    def test_export_import_roundtrip_and_reset_revision(self):
        revision, workspace = self.stage()
        restored = Workspace.model_validate_json(workspace.model_dump_json())
        new_revision, actual = self.bench.replace(revision, restored)
        self.assertEqual(actual, workspace)
        reset_revision, blank = self.bench.reset(new_revision)
        self.assertGreater(reset_revision, new_revision)
        self.assertEqual(blank, Workspace())

    def test_invalid_import_and_corrupt_storage_do_not_reset(self):
        data = self.workspace.model_dump(mode="json")
        data["schema_version"] = 3
        with self.assertRaises(ValidationError):
            Workspace.model_validate(data)
        self.store.payload = b"not a workspace"
        with self.assertRaises(StorageError):
            self.bench.load()
        self.assertEqual(self.store.payload, b"not a workspace")

    def test_duplicate_ids_and_future_versions_rejected(self):
        with self.assertRaises(ValidationError):
            Workspace(messages=[self.message, self.message])
        with self.assertRaises(ValidationError):
            ContextSelection(message_ids=[self.message.id, self.message.id])
        with self.assertRaises(UserError):
            self.bench.context(self.revision, ContextSelection())
        with self.assertRaises(MissingRecord):
            self.bench.delete_statement(self.revision, self.message.id)

    def test_connected_paths_survive_review_and_export_without_changing_the_map(self):
        revision, workspace = self.bench.save_statement(self.revision, StatementDraft(
            text="Would consider lower pay for a four-day week", domain="money", source="explicit",
            condition="Only if the schedule can be confirmed",
            evidence=[Evidence(message_id=self.message.id, quote="I could accept lower pay for a four-day week.")],
        ))
        notes = workspace.statements
        version = workspace.map_version
        statement_id = notes[0].id
        paths = [
            OpportunityDraft(
                title="Explore repair work near home", kind="career", statement_ids=[statement_id],
                rationale="Connect repair interests with more time outside work.\nAn ordinary week might include practical work and a shorter commute; both are assumptions.",
                tradeoffs=["Reduced hours may reduce financial breathing room"],
                unknowns=["Would an attainable schedule leave enough income?"],
                next_steps=["Compare one real schedule with household expenses before pursuing a role"],
            ),
            OpportunityDraft(
                title="Keep repair as a community interest", kind="lifestyle", statement_ids=[statement_id],
                rationale="Explore repair participation separately from earning a living.\nAn ordinary week might include an occasional group session if accessible; attendance is not paid work.",
                tradeoffs=["Free time could become another obligation"],
                unknowns=["Would recurring participation restore energy or consume it?"],
                next_steps=["Look for a public repair group and check its schedule before deciding to visit"],
            ),
        ]
        selection = ContextSelection(task="discover", message_ids=[self.message.id], statement_ids=[statement_id])
        revision, workspace = self.bench.propose(revision, selection, ModelReply(
            assistant_text="Two different ways to connect repair interests and time outside work", opportunities=paths,
        ), "synthetic")
        self.assertEqual(workspace.opportunities, [])
        revision, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(
            revision=revision, opportunity_indices=[0, 1],
        ))
        brief = BriefDraft(
            title="Two repair-related paths",
            summary="The choice is whether repair belongs in paid work or outside it; neither path is established as viable.",
            directions=[f"{path.title}\n{path.rationale}\nMain tradeoff: {path.tradeoffs[0]}" for path in paths],
            open_questions=[f"{path.title}: {path.unknowns[0]}" for path in paths],
            next_steps=[f"{path.title}: {path.next_steps[0]}" for path in paths],
            search_queries=["community repair group", "repair work flexible schedule"],
        )
        selection.task = "brief"
        self.assertNotIn("opportunities", self.bench.context(revision, selection).model_dump())
        revision, workspace = self.bench.propose(revision, selection, ModelReply(
            assistant_text="A provisional comparison", brief=brief,
        ), "synthetic")
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        restored = Workspace.model_validate_json(workspace.model_dump_json())
        self.assertEqual(restored, workspace)
        self.assertEqual(restored.statements, notes)
        self.assertEqual(restored.map_version, version)
        self.assertTrue(all(path.status == "hypothesis" for path in restored.opportunities))
        assert restored.brief is not None
        self.assertEqual(restored.brief.directions, brief.directions)
        self.assertEqual(restored.brief.open_questions, brief.open_questions)
        self.assertEqual(restored.brief.next_steps, brief.next_steps)
        report = render_brief(restored)
        for text in (
            "## What matters and what differs", "### Path 1", "### Path 2", "An ordinary week might",
            "## Questions that could change the choice", "## A small investigation plan",
            "1. Explore repair work near home", "2. Keep repair as a community interest",
            "Only if the schedule can be confirmed", "## Saved possibilities — separate reference",
            "not automatically supplied", "Person Map connections (current notes)", statement_id.replace("-", "\\-"),
        ):
            self.assertIn(text, report)

    def test_path_sections_and_numbered_steps_keep_untrusted_markup_inert(self):
        attack = '<script>alert(1)</script> ![image](https://example.invalid/track)'
        reply = ModelReply(assistant_text="A draft", brief=BriefDraft(
            title="A plan", summary="A tentative comparison", directions=[attack],
            open_questions=[attack], next_steps=["Inspect locally\n" + attack],
        ))
        revision, workspace = self.stage(reply)
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        report = render_brief(workspace)
        self.assertIn("### Path 1", report)
        self.assertIn("1. Inspect locally\n   &lt;script&gt;", report)
        self.assertNotIn("<script>", report)
        self.assertNotIn("![image]", report)

    def test_existing_brief_without_paths_does_not_invent_detail(self):
        reply = ModelReply(assistant_text="Summary", brief=BriefDraft(title="Earlier plan", summary="Earlier summary"))
        revision, workspace = self.stage(reply)
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        report = render_brief(workspace)
        self.assertIn("Earlier summary", report)
        self.assertIn("No paths listed yet", report)
        self.assertNotIn("### Path 1", report)

    def test_markdown_export_keeps_html_and_images_inert(self):
        reply = ModelReply(assistant_text="Summary", brief=BriefDraft(title="Plan", summary='<script>alert(1)</script> ![image](https://example.invalid/track)'))
        revision, workspace = self.stage(reply)
        _, workspace = self.bench.accept(workspace.proposals[0].id, AcceptRequest(revision=revision, include_brief=True))
        report = render_brief(workspace)
        self.assertNotIn("<script>", report)
        self.assertNotIn("![image]", report)
        self.assertIn("hypotheses", report)


if __name__ == "__main__":
    unittest.main()
