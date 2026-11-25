# agents/ceo_agent.py
#
# PHASE 2 — Full pipeline: decompose → review → engineer → market → qa → Slack summary.

import json
import os
import time

import requests

from agents.marketing_agent import _slack_channel_id
from google import genai

from message_bus import send_message, receive_messages


class CEOAgent:
    """
    Orchestrates the entire LaunchMind pipeline.

    Responsibilities
    ----------------
    1. Decompose the startup idea into tasks for each specialist agent.
    2. Review the Product spec (with up to 2 revision cycles).
    3. Dispatch approved spec to Engineer and Marketing agents.
    4. Run QA review and collect pass/fail + PR comments.
    5. Compile and post the final launch summary to Slack #launches.
    """

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise EnvironmentError(
                "[CEO] GEMINI_API_KEY is not set.\n"
                "Please add it to your .env file and try again."
            )
        self.client = genai.Client(api_key=api_key)
        self.agent_name = "ceo"

    # ------------------------------------------------------------------
    # Internal Gemini helper
    # ------------------------------------------------------------------

    def _call_gemini(self, system_prompt: str, user_prompt: str, retries: int = 1) -> str:
        """Call the Gemini API with simple retry logic."""
        for attempt in range(retries + 1):
            try:
                response = self.client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user_prompt,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_prompt,
                    ),
                )
                return response.text
            except Exception as exc:
                print(f"[CEO] Gemini API error (attempt {attempt + 1}): {exc}")
                if (
                    "429" in str(exc)
                    or "RESOURCE_EXHAUSTED" in str(exc)
                    or "503" in str(exc)
                    or "UNAVAILABLE" in str(exc)
                ):
                    print("[CEO] Gemini unavailable/quota hit. 🔄 Falling back to Hugging Face...")
                    return self._call_hf(system_prompt, user_prompt)

                if attempt < retries:
                    print("[CEO] Retrying Gemini call...")
                    time.sleep(2)
                else:
                    raise


    def _call_hf(self, system_prompt: str, user_prompt: str) -> str:
        from huggingface_hub import InferenceClient
        client = InferenceClient(api_key=os.environ.get("HF_API_KEY", ""))
        response = client.chat_completion(
            model="meta-llama/Llama-3.3-70B-Instruct",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4000
        )
        return response.choices[0].message.content or ""
    # Public API
    # ------------------------------------------------------------------

    def run(self, startup_idea: str) -> None:
        """Main entry point — runs the entire multi-agent pipeline."""
        print("\n" + "=" * 60)
        print("[CEO] LaunchMind pipeline started.")
        print(f"[CEO] Startup idea: {startup_idea}")
        print("=" * 60 + "\n")

        # Step 1: Decompose idea and dispatch tasks
        tasks = self.decompose_idea(startup_idea)

        # Step 2: Product agent writes spec
        send_message(
            from_agent=self.agent_name,
            to_agent="product",
            message_type="task",
            payload={
                "idea": startup_idea,
                "focus": tasks.get("product_task", ""),
            },
        )

        from agents.product_agent import ProductAgent

        product_agent = ProductAgent()
        product_agent.run()

        # Collect product spec result
        product_result_msgs = receive_messages(self.agent_name)
        product_spec = None
        for msg in product_result_msgs:
            if msg["from_agent"] == "product" and msg["message_type"] == "result":
                product_spec = msg["payload"].get("spec")
                break

        if not product_spec:
            print("[CEO] ERROR: Did not receive a product spec from ProductAgent.")
            return

        # Step 3: Review spec with up to 2 revision cycles
        product_spec = self.review_product_spec(product_spec, startup_idea, tasks["product_task"])

        # Step 4: Dispatch to Engineer
        print("[CEO] Dispatching approved spec to EngineerAgent...")
        send_message(
            from_agent=self.agent_name,
            to_agent="engineer",
            message_type="task",
            payload={
                "spec": product_spec,
                "engineer_task": tasks.get("engineer_task", ""),
            },
        )

        from agents.engineer_agent import EngineerAgent

        engineer_agent = EngineerAgent()
        engineer_agent.run()

        # Collect engineer result
        engineer_result_msgs = receive_messages(self.agent_name)
        pr_url = ""
        issue_url = ""
        html_saved_to = ""
        for msg in engineer_result_msgs:
            if msg["from_agent"] == "engineer" and msg["message_type"] == "result":
                pr_url = msg["payload"].get("pr_url", "")
                issue_url = msg["payload"].get("issue_url", "")
                html_saved_to = msg["payload"].get("html_saved_to", "")
                break

        # Step 5: Dispatch to Marketing
        print("[CEO] Dispatching approved spec to MarketingAgent...")
        send_message(
            from_agent=self.agent_name,
            to_agent="marketing",
            message_type="task",
            payload={
                "spec": product_spec,
                "marketing_task": tasks.get("marketing_task", ""),
                "pr_url": pr_url,
            },
        )

        from agents.marketing_agent import MarketingAgent

        marketing_agent = MarketingAgent()
        marketing_agent.run()

        # Collect marketing result
        marketing_result_msgs = receive_messages(self.agent_name)
        marketing_copy = {}
        for msg in marketing_result_msgs:
            if msg["from_agent"] == "marketing" and msg["message_type"] == "result":
                marketing_copy = msg["payload"].get("copy", {})
                break

        # Step 6: Dispatch to QA agent
        print("[CEO] Dispatching outputs to QAAgent for review...")
        send_message(
            from_agent=self.agent_name,
            to_agent="qa",
            message_type="task",
            payload={
                "spec": product_spec,
                "marketing_copy": marketing_copy,
                "pr_url": pr_url,
                "html_path": html_saved_to,
            },
        )

        from agents.qa_agent import QAAgent

        qa_agent = QAAgent()
        qa_agent.run()

        qa_result_msgs = receive_messages(self.agent_name)
        qa_report = {
            "verdict": "fail",
            "issues": ["No QA report received from QAAgent."],
            "comments_posted": 0,
        }
        for msg in qa_result_msgs:
            if msg["from_agent"] == "qa" and msg["message_type"] == "result":
                qa_report = msg["payload"]
                break

        print(f"[CEO] QA verdict: {qa_report.get('verdict', 'unknown')}")
        issues = qa_report.get("issues", [])
        if issues:
            print("[CEO] QA issues:")
            for i, issue in enumerate(issues, start=1):
                print(f"   {i}. {issue}")

        # Step 7: Compile and post final Slack summary
        print("[CEO] Compiling final launch summary...")
        self.compile_final_summary(product_spec, pr_url, issue_url, marketing_copy, qa_report)

        print("\n[CEO] Pipeline complete. 🎉")

    # ------------------------------------------------------------------
    # decompose_idea
    # ------------------------------------------------------------------

    def decompose_idea(self, startup_idea: str) -> dict:
        """
        Calls Gemini to break the idea into specific tasks.
        Returns a dict with keys: product_task, engineer_task, marketing_task.
        """
        print("[CEO] Decomposing startup idea...")

        system_prompt = (
            "You are an experienced startup CEO. Your job is to break down a startup idea "
            "into specific tasks for your team. You have a Product Manager, a Software "
            "Engineer, and a Marketing Expert on your team. Given a startup idea, generate "
            "specific focused tasks for each team member. Return ONLY a valid JSON object "
            "with no extra text, no markdown, no backticks. Format:\n"
            "{\n"
            '  "product_task": "specific instruction for product manager",\n'
            '  "engineer_task": "specific instruction for engineer",\n'
            '  "marketing_task": "specific instruction for marketer"\n'
            "}"
        )

        user_prompt = (
            f"Here is the startup idea: {startup_idea}\n"
            "Break this into specific tasks for each team member."
        )

        raw = self._call_gemini(system_prompt, user_prompt)

        try:
            tasks = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[CEO] JSON parse error on decompose_idea: {exc}")
            print(f"[CEO] Raw response:\n{raw}")
            tasks = {
                "product_task": "Create a detailed product specification for this startup idea.",
                "engineer_task": "Build a landing page for this startup.",
                "marketing_task": "Write compelling marketing copy for this startup.",
            }

        print(f"[CEO] Tasks decomposed: {list(tasks.keys())}")
        print(f"[CEO] Decision log — decompose_idea:")
        for k, v in tasks.items():
            print(f"   {k}: {v}")
        return tasks

    # ------------------------------------------------------------------
    # review_product_spec
    # ------------------------------------------------------------------

    def review_product_spec(
        self,
        spec: dict,
        startup_idea: str,
        product_task: str,
        max_revisions: int = 2,
    ) -> dict:
        """
        Reviews the spec from ProductAgent.
        Sends revision_request messages if needed (up to max_revisions times).
        Returns the (possibly revised) approved spec.
        """
        from agents.product_agent import ProductAgent

        product_agent = ProductAgent()
        revision_count = 0

        system_prompt = (
            "You are a strict and experienced startup CEO reviewing a product specification "
            "written by your Product Manager. You must ensure the spec is detailed, specific, "
            "and actionable enough for a developer to build from. Be critical. Do not accept "
            "vague or generic output."
        )

        while revision_count <= max_revisions:
            print(f"[CEO] Reviewing product spec (revision cycle {revision_count})...")

            spec_str = json.dumps(spec, indent=2)
            user_prompt = (
                f"Review this product specification:\n{spec_str}\n\n"
                "Answer with EXACTLY one of these two options:\n"
                "1. If the spec is good enough: respond with just the word APPROVED\n"
                "2. If the spec needs work: respond with NEEDS_REVISION followed by a "
                "numbered list of exactly what needs to be fixed.\n\n"
                "Be strict. Generic personas or vague features are not acceptable."
            )

            review = self._call_gemini(system_prompt, user_prompt)
            # Log the full LLM reasoning so the evaluator can see WHY the CEO decided
            print(f"[CEO] Decision log — review (cycle {revision_count}):")
            print(f"   {review.strip()}")

            if review.strip().upper().startswith("APPROVED"):
                print("[CEO] Product spec APPROVED.")
                return spec

            if revision_count >= max_revisions:
                print(f"[CEO] Max revisions ({max_revisions}) reached. Proceeding with current spec.")
                return spec

            # Send revision request to product agent
            feedback = review.replace("NEEDS_REVISION", "").strip()
            print(f"[CEO] Sending revision request to ProductAgent (revision {revision_count + 1})...")

            send_message(
                from_agent=self.agent_name,
                to_agent="product",
                message_type="revision_request",
                payload={
                    "idea": startup_idea,
                    "focus": product_task,
                    "feedback": feedback,
                    "previous_spec": spec,
                },
            )

            product_agent.run()

            # Collect revised spec
            revised_msgs = receive_messages(self.agent_name)
            for msg in revised_msgs:
                if msg["from_agent"] == "product" and msg["message_type"] == "result":
                    spec = msg["payload"].get("spec", spec)
                    break

            revision_count += 1

        return spec

    # ------------------------------------------------------------------
    # compile_final_summary
    # ------------------------------------------------------------------

    def compile_final_summary(
        self,
        product_spec: dict,
        pr_url: str,
        issue_url: str,
        marketing_copy: dict,
        qa_report: dict,
    ) -> None:
        """
        Calls Gemini to write a Slack launch summary, then posts it to #launches.
        """
        print("[CEO] Generating final Slack launch summary via Gemini...")

        system_prompt = (
            "You are a startup CEO writing a final launch summary for your team on Slack. "
            "Be enthusiastic but professional. Keep it concise."
        )

        tagline = marketing_copy.get("tagline", "Built with LaunchMind")
        startup_name = marketing_copy.get("startup_name", "Our Startup")

        user_prompt = (
            f"Write a Slack launch summary for our startup based on:\n"
            f"Product spec: {json.dumps(product_spec)}\n"
            f"GitHub PR: {pr_url}\n"
            f"GitHub Issue: {issue_url}\n"
            f"QA report: {json.dumps(qa_report)}\n"
            f"Marketing tagline: {tagline}\n\n"
            "Include: startup name, what it does, the tagline, links to GitHub PR and issue, "
            "QA verdict and top issues (if any), and a congratulations message to the team.\n"
            "Return plain text only, no JSON."
        )

        summary_text = self._call_gemini(system_prompt, user_prompt)
        print("[CEO] Slack summary generated.")
        print(f"\n--- FINAL SUMMARY ---\n{summary_text}\n---------------------\n")

        # ── Post to Slack #launches ──────────────────────────────────────
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        if slack_token:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"Final Launch Summary: {startup_name}",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": summary_text},
                },
            ]
            channel_id = _slack_channel_id(slack_token, preferred_name="launches")
            if not channel_id:
                print("[CEO] Bot is not a member of any Slack channel — final summary printed above only.")
                return

            payload = {
                "channel": channel_id,
                "text": f"Final Launch Summary: {startup_name}",
                "blocks": blocks,
            }
            headers = {
                "Authorization": f"Bearer {slack_token}",
                "Content-Type": "application/json",
            }
            try:
                resp = requests.post(
                    "https://slack.com/api/chat.postMessage",
                    json=payload,
                    headers=headers,
                    timeout=15,
                )
                data = resp.json()
                if data.get("ok"):
                    print("[CEO] ✅  Final summary posted to Slack #launches ✓")
                else:
                    print(f"[CEO] ❌  Slack post failed: {data.get('error', 'unknown')}")
            except Exception as exc:
                print(f"[CEO] ❌  Slack request error: {exc}")
        else:
            print("[CEO] ⚠️  SLACK_BOT_TOKEN not set — final summary printed above only.")

        send_message(
            from_agent=self.agent_name,
            to_agent="ceo",
            message_type="confirmation",
            payload={
                "summary": summary_text,
                "slack_posted": bool(slack_token),
            },
        )
