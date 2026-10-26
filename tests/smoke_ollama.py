"""Opt-in live Lyra check using ONLY synthetic data and a temporary encrypted DB.

Run: python tests/smoke_ollama.py --model Lyra:latest
Use --chat-only for one plain-text streaming request instead of two structured requests.
Not included in unittest discovery.
"""

import argparse
import tempfile
from pathlib import Path
from time import monotonic

from jobwolf.conversation import select_chat_context
from jobwolf.models import AcceptRequest, ContextSelection
from jobwolf.providers import OllamaProvider
from jobwolf.service import Bench, render_brief, validate_reply
from jobwolf.storage import SQLiteStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="Lyra:latest")
    parser.add_argument("--chat-only", action="store_true", help="Check plain-text streaming without structured generation")
    args = parser.parse_args()
    provider = OllamaProvider(timeout=150)
    if args.model not in provider.list_models():
        raise SystemExit("The requested local model is not installed. No download was attempted.")
    with tempfile.TemporaryDirectory(prefix="jobwolf-synthetic-") as directory:
        bench = Bench(SQLiteStore(Path(directory) / "workspace.sqlite3"))
        if args.chat_only:
            revision, workspace = bench.add_message(0,
                "Synthetic test: I like fixing bicycles and would like more time outdoors. "
                "Please reflect briefly and ask one useful follow-up question."
            )
            message_id = workspace.messages[-1].id
            _, context = select_chat_context(workspace, message_id)
            started = monotonic()
            chunks = []
            for chunk in provider.stream_chat(args.model, context):
                if not chunks:
                    print(f"First real text chunk after {monotonic() - started:.1f}s.", flush=True)
                chunks.append(chunk)
            assert chunks, "No visible text was streamed."
            _, result = bench.answer_chat(revision, message_id, "".join(chunks))
            assert len(result.messages) == 2 and result.messages[-1].role == "assistant"
            assert not result.statements and not result.proposals and not result.opportunities and result.brief is None
            print(f"PASS: {len(chunks)} streamed text chunks, {len(result.messages[-1].text)} characters, complete reply saved in {monotonic() - started:.1f}s.")
            print("Synthetic temporary workspace only; no Person Map changes or proposals. Removed on exit.")
            return
        revision, workspace = bench.add_message(0,
            "I enjoy fixing unfamiliar practical problems. I would trade some pay for a four-day week, "
            "but I have not settled on a minimum income. I would consider moving within the contiguous USA. "
            "Please reflect this back and propose a few Person Map statements, keeping the pay/schedule tradeoff conditional."
        )
        selection = ContextSelection(task="conversation", message_ids=[workspace.messages[0].id])
        context = bench.context(revision, selection)
        reply = provider.generate(args.model, context)
        try:
            validate_reply(reply, context)
        except Exception:
            print("Synthetic reply for diagnosis (never use real user context in this test):")
            print(reply.model_dump_json(indent=2))
            raise
        if not reply.proposed_statements:
            raise SystemExit("The model returned no Person Map proposals; the extraction check is inconclusive.")
        revision, workspace = bench.propose(revision, selection, reply, "synthetic Ollama smoke test")
        revision, workspace = bench.accept(workspace.proposals[0].id, AcceptRequest(
            revision=revision, statement_indices=list(range(len(reply.proposed_statements))),
            opportunity_indices=list(range(len(reply.opportunities))),
        ))
        print(f"PASS: reviewed {len(workspace.statements)} schema-valid, evidence-checked map proposals.", flush=True)
        selection = ContextSelection(task="brief", message_ids=[workspace.messages[0].id], statement_ids=[s.id for s in workspace.statements])
        context = bench.context(revision, selection)
        reply = provider.generate(args.model, context)
        validate_reply(reply, context)
        revision, workspace = bench.propose(revision, selection, reply, "synthetic Ollama smoke test")
        _, workspace = bench.accept(workspace.proposals[0].id, AcceptRequest(
            revision=revision, opportunity_indices=list(range(len(reply.opportunities))), include_brief=True,
        ))
        report = render_brief(workspace)
        assert "Current Person Map" in report and workspace.brief is not None
        print(f"PASS: accepted a brief and exported {len(report)} characters of Markdown.")
        print("Synthetic workspace removed on exit. Your real workspace was never opened.")


if __name__ == "__main__":
    main()
