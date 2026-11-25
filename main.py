# main.py
#
# LaunchMind — Full Phase 2 entry point
# Pipeline: CEO → Product (with review) → Engineer (HTML + GitHub) → Marketing (Email + Slack)
# Ends with CEO posting a final Slack summary and printing the full message log.
from __future__ import annotations

import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

# Load .env before any agent is imported
load_dotenv()

# ── Validate required key ────────────────────────────────────────────────────
if not os.environ.get("GEMINI_API_KEY"):
    print(
        "\n❌  GEMINI_API_KEY is not set in your .env file.\n"
        "   Open .env, add your Gemini API key, then run again.\n"
    )
    sys.exit(1)


def print_message_log(full_message_log: list) -> None:
    """Pretty-print the full agent conversation history for the evaluator."""
    print("\n" + "=" * 70)
    print("  FULL AGENT MESSAGE LOG")
    print("=" * 70)

    if not full_message_log:
        print("  (no messages recorded)")
    else:
        for i, msg in enumerate(full_message_log, start=1):
            print(
                f"\n[{i}] {msg['from_agent'].upper()} → {msg['to_agent'].upper()} "
                f"| type={msg['message_type']} | id={msg['message_id'][:8]}..."
            )
            print(f"     timestamp : {msg['timestamp']}")
            if msg.get("parent_message_id"):
                print(f"     reply-to  : {msg['parent_message_id'][:8]}...")
            try:
                payload_str = json.dumps(msg["payload"], indent=6)
                if len(payload_str) > 600:
                    payload_str = payload_str[:597] + "..."
                print(f"     payload   :\n{payload_str}")
            except Exception:
                print(f"     payload   : {str(msg['payload'])[:600]}")

    print("\n" + "=" * 70 + "\n")


def _payload_preview(payload: Any) -> str:
    """Build a compact one-line payload preview for text logs."""
    if isinstance(payload, dict):
        keys = list(payload.keys())
        return ", ".join(keys) if keys else "(empty dict)"
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    return str(payload)[:100]


def save_message_log_txt(full_message_log: list, path: str = "message_log.txt") -> None:
    """
    Save a detailed, human-readable log showing who talked to whom and what happened.
    """
    lines: list[str] = []
    lines.append("=" * 90)
    lines.append("LaunchMind - Complete Agent Communication Log")
    lines.append("=" * 90)
    lines.append(f"Total messages: {len(full_message_log)}")
    lines.append("")

    if not full_message_log:
        lines.append("(no messages recorded)")
    else:
        for i, msg in enumerate(full_message_log, start=1):
            sender = msg.get("from_agent", "unknown")
            receiver = msg.get("to_agent", "unknown")
            message_type = msg.get("message_type", "unknown")
            message_id = msg.get("message_id", "")
            timestamp = msg.get("timestamp", "")
            parent_id = msg.get("parent_message_id")
            payload = msg.get("payload", {})

            lines.append("-" * 90)
            lines.append(f"[{i}] {timestamp}")
            lines.append(f"FLOW       : {sender.upper()} -> {receiver.upper()}")
            lines.append(f"TYPE       : {message_type}")
            lines.append(f"MESSAGE ID : {message_id}")
            if parent_id:
                lines.append(f"REPLY TO   : {parent_id}")
            lines.append(f"PAYLOAD    : {_payload_preview(payload)}")
            lines.append("DETAILS    :")
            lines.append(json.dumps(payload, indent=2, ensure_ascii=True))
            lines.append("")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"📝  Human-readable message log saved to: {path}")
    except Exception as exc:
        print(f"⚠️  Could not save text log '{path}': {exc}")


def main() -> None:
    # ── Banner ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  🚀  LaunchMind — AI Multi-Agent Launch System")
    print("  Full pipeline: Product → GitHub → Email → Slack")
    print("=" * 60)

    # ── Warn about optional integrations ─────────────────────────────────────
    missing = []
    for var in ["GITHUB_TOKEN", "GITHUB_REPO", "SLACK_BOT_TOKEN", "BREVO_API_KEY",
                "BREVO_FROM_EMAIL", "TEST_EMAIL"]:
        if not os.environ.get(var):
            missing.append(var)

    if missing:
        print("\n⚠️  The following optional env vars are not set (integrations will be skipped):")
        for v in missing:
            print(f"   • {v}")
        print("   Add them to .env to enable full GitHub / Slack / Email functionality.\n")

    # ── Get startup idea ─────────────────────────────────────────────────────
    startup_idea = input("\nEnter your startup idea: ").strip()
    if not startup_idea:
        print("No idea entered. Exiting.")
        sys.exit(1)

    print(f"\n✅  Idea received: '{startup_idea}'")
    print("  Starting the multi-agent pipeline...\n")

    # ── Run the full pipeline via CEOAgent ────────────────────────────────────
    from agents.ceo_agent import CEOAgent
    import message_bus

    ceo = CEOAgent()
    ceo.run(startup_idea)

    # ── Print full message log for evaluator ──────────────────────────────────
    print_message_log(message_bus.full_message_log)

    # ── Save logs to a JSON file ──────────────────────────────────────────────
    log_file_path = "message_log.json"
    try:
        with open(log_file_path, "w", encoding="utf-8") as f:
            json.dump(message_bus.full_message_log, f, indent=4)
        print(f"📄  Full message log saved locally to: {log_file_path}")
    except Exception as e:
        print(f"⚠️  Could not save message_log.json: {e}")

    # ── Save logs to human-readable TXT file ──────────────────────────────────
    save_message_log_txt(message_bus.full_message_log, path="message_log.txt")

    print("\n🎉  LaunchMind pipeline complete!\n")


if __name__ == "__main__":
    main()
