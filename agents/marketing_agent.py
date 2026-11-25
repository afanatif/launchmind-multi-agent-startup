# agents/marketing_agent.py
#
# LaunchMind — Marketing Agent
# Responsibilities: generate copy, send Brevo email, post Block Kit card to Slack.

import json
import os
import time

import requests
from google import genai

from message_bus import send_message, receive_messages

# ---------------------------------------------------------------------------
# Slack helper — resolves a channel name to its ID so posting always works
# ---------------------------------------------------------------------------

def _slack_channel_id(token: str, preferred_name: str = "new-channel") -> str:
    """
    Return the channel ID for *preferred_name*.
    Falls back to any channel the bot is a member of.
    Returns '' if nothing is found.
    """
    resp = requests.get(
        "https://slack.com/api/conversations.list",
        headers={"Authorization": f"Bearer {token}"},
        params={"types": "public_channel,private_channel", "limit": 100},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"[MARKETING] Slack conversations.list error: {data.get('error')}")
        return ""

    channels = data.get("channels", [])
    # 1st pass — preferred name, must be a member
    for ch in channels:
        if ch["name"] == preferred_name and ch.get("is_member"):
            return ch["id"]
    # 2nd pass — any channel bot is a member of
    for ch in channels:
        if ch.get("is_member"):
            return ch["id"]
    return ""


class MarketingAgent:
    """
    Generates marketing copy, sends a cold-outreach email via Brevo,
    and posts a Block Kit launch card to Slack.
    """

    def __init__(self):
        self.client     = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        self.agent_name = "marketing"

    # ------------------------------------------------------------------
    # Internal LLM helpers
    # ------------------------------------------------------------------

    def _call_gemini(self, system_prompt: str, user_prompt: str, retries: int = 1) -> str:
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
                print(f"[MARKETING] Gemini API error (attempt {attempt + 1}): {exc}")
                if (
                    "429" in str(exc)
                    or "RESOURCE_EXHAUSTED" in str(exc)
                    or "503" in str(exc)
                    or "UNAVAILABLE" in str(exc)
                ):
                    print("[MARKETING] Gemini unavailable/quota hit — falling back to Hugging Face...")
                    return self._call_hf(system_prompt, user_prompt)
                if attempt < retries:
                    print("[MARKETING] Retrying Gemini call...")
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
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=4000,
        )
        return response.choices[0].message.content or ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        messages = receive_messages(self.agent_name)
        if not messages:
            print("[MARKETING] No messages in inbox.")
            return

        for msg in messages:
            if msg["message_type"] == "task":
                print("[MARKETING] Received task from CEO.")
                self.market(msg["payload"], msg["message_id"])
            else:
                print(f"[MARKETING] Unknown message type: {msg['message_type']}. Skipping.")

    # ------------------------------------------------------------------
    # market
    # ------------------------------------------------------------------

    def market(self, payload: dict, parent_id: str) -> None:
        spec   = payload.get("spec", {})
        pr_url = payload.get("pr_url", "")

        # Step 1: Generate all copy
        copy = self.generate_copy(spec)

        # Print preview to terminal
        print("\n" + "─" * 55)
        print("  MARKETING COPY")
        print("─" * 55)
        print(f"  Startup name : {copy.get('startup_name', '')}")
        print(f"  Tagline      : {copy.get('tagline', '')}")
        print(f"  Description  : {copy.get('short_description', '')}")
        print(f"\n  Email subject: {copy.get('cold_email', {}).get('subject', '')}")
        print(f"  Email body   :\n{copy.get('cold_email', {}).get('body', '')}")
        print(f"\n  Twitter  : {copy.get('social_posts', {}).get('twitter', '')}")
        print(f"  LinkedIn : {copy.get('social_posts', {}).get('linkedin', '')}")
        print(f"  Instagram: {copy.get('social_posts', {}).get('instagram', '')}")
        print("─" * 55 + "\n")

        # Step 2: Send cold-outreach email via Brevo
        self.send_email(copy)

        # Step 3: Post to Slack
        self.post_to_slack(copy, pr_url)

        # Step 4: Report back to CEO
        print("[MARKETING] Reporting generated copy to CEO...")
        send_message(
            from_agent=self.agent_name,
            to_agent="ceo",
            message_type="result",
            payload={"copy": copy},
            parent_id=parent_id,
        )

    # ------------------------------------------------------------------
    # generate_copy
    # ------------------------------------------------------------------

    def generate_copy(self, spec: dict) -> dict:
        print("[MARKETING] Generating marketing copy via Gemini...")

        system_prompt = (
            "You are an expert startup marketing copywriter. You write punchy, "
            "compelling copy that speaks directly to user pain points. You never use "
            "corporate jargon. Your taglines are memorable and under 10 words. "
            "Your emails feel personal and genuine, not salesy."
        )

        user_prompt = (
            f"Generate complete marketing copy for this startup:\n"
            f"Value proposition: {spec.get('value_proposition', '')}\n"
            f"Target personas: {json.dumps(spec.get('personas', []))}\n"
            f"Core features: {json.dumps(spec.get('features', []))}\n\n"
            "Return ONLY a valid JSON object with NO extra text, NO markdown, NO backticks:\n"
            "{\n"
            '  "startup_name": "a short catchy name for the startup",\n'
            '  "tagline": "under 10 words, punchy and memorable",\n'
            '  "short_description": "2-3 sentences for landing page",\n'
            '  "cold_email": {\n'
            '    "subject": "email subject line",\n'
            '    "body": "full email body, personal tone, 150-200 words, ends with clear CTA"\n'
            "  },\n"
            '  "social_posts": {\n'
            '    "twitter": "under 280 characters, include hashtags",\n'
            '    "linkedin": "3-4 sentences, professional tone",\n'
            '    "instagram": "2-3 sentences, casual tone, include emojis"\n'
            "  }\n"
            "}"
        )

        raw = self._call_gemini(system_prompt, user_prompt)

        try:
            copy = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[MARKETING] JSON parse error: {exc}")
            print(f"[MARKETING] Raw response:\n{raw}")
            copy = {
                "startup_name": "LaunchMind Startup",
                "tagline": "Built for the future.",
                "short_description": spec.get("value_proposition", ""),
                "cold_email": {
                    "subject": "Check out our new product",
                    "body": (
                        f"Hi,\n\nWe built something for you:\n"
                        f"{spec.get('value_proposition', '')}\n\n"
                        "Reply to learn more.\n\nBest,\nThe Team"
                    ),
                },
                "social_posts": {
                    "twitter":   f"{spec.get('value_proposition', '')} #startup #launch",
                    "linkedin":  spec.get("value_proposition", ""),
                    "instagram": f"{spec.get('value_proposition', '')}",
                },
            }

        print(f"[MARKETING] Copy generated for '{copy.get('startup_name', 'unknown')}' ✓")
        return copy

    # ------------------------------------------------------------------
    # send_email  (Brevo)
    # ------------------------------------------------------------------

    def send_email(self, copy: dict) -> None:
        """Send cold outreach email via Brevo REST API."""
        api_key    = os.environ.get("BREVO_API_KEY", "")
        from_email = os.environ.get("BREVO_FROM_EMAIL", "")
        to_email   = os.environ.get("TEST_EMAIL", "")

        if not all([api_key, from_email, to_email]):
            print(
                "[MARKETING] Brevo env vars not set "
                "(BREVO_API_KEY / BREVO_FROM_EMAIL / TEST_EMAIL) — skipping email."
            )
            return

        print(f"[MARKETING] Sending cold-outreach email to {to_email}...")

        subject = copy.get("cold_email", {}).get("subject", "Hello from LaunchMind")
        body    = copy.get("cold_email", {}).get("body", "")
        startup_name = copy.get("startup_name", "LaunchMind")

        html_template = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <style>
            body {{
              font-family: 'Inter', 'Helvetica Neue', Helvetica, Arial, sans-serif;
              background-color: #f7f9fc;
              margin: 0;
              padding: 0;
              -webkit-font-smoothing: antialiased;
            }}
            .email-wrapper {{
              width: 100%;
              background-color: #f7f9fc;
              padding: 40px 0;
            }}
            .email-container {{
              max-width: 600px;
              margin: 0 auto;
              background-color: #ffffff;
              border-radius: 12px;
              box-shadow: 0 4px 15px rgba(0, 0, 0, 0.05);
              overflow: hidden;
            }}
            .header {{
              background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
              padding: 35px 20px;
              text-align: center;
            }}
            .header h1 {{
              margin: 0;
              font-size: 28px;
              color: #ffffff;
              letter-spacing: 0.5px;
            }}
            .body-content {{
              padding: 40px 35px;
              color: #334155;
              line-height: 1.7;
              font-size: 16px;
            }}
            .footer {{
              background-color: #f8fafc;
              padding: 25px;
              text-align: center;
              font-size: 13px;
              color: #64748b;
              border-top: 1px solid #e2e8f0;
            }}
            .footer p {{
              margin: 5px 0;
            }}
          </style>
        </head>
        <body>
          <div class="email-wrapper">
            <div class="email-container">
              <div class="header">
                <h1>{startup_name}</h1>
              </div>
              <div class="body-content">
                {body.replace(chr(10), '<br>')}
              </div>
              <div class="footer">
                <p>Sent by <strong>LaunchMind AI</strong> — Built for the future.</p>
                <p>Autonomous Multi-Agent System Notification</p>
              </div>
            </div>
          </div>
        </body>
        </html>
        """

        payload = {
            "sender": {"email": from_email},
            "to":     [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_template,
        }

        headers = {
            "api-key":       api_key,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

        try:
            resp = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 201:
                print(f"[MARKETING] Email sent to {to_email} ✓")
            else:
                print(f"[MARKETING] Brevo error ({resp.status_code}): {resp.text}")
        except Exception as exc:
            print(f"[MARKETING] Email request error: {exc}")

    # ------------------------------------------------------------------
    # post_to_slack  (Block Kit)
    # ------------------------------------------------------------------

    def post_to_slack(self, copy: dict, pr_url: str) -> None:
        """Post a Block Kit launch card to the Slack channel."""
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not slack_token:
            print("[MARKETING] SLACK_BOT_TOKEN not set — skipping Slack post.")
            return

        # Resolve the channel ID dynamically
        channel_id = _slack_channel_id(slack_token, preferred_name="launches")
        if not channel_id:
            print("[MARKETING] Bot is not a member of any Slack channel — skipping post.")
            return

        print(f"[MARKETING] Posting Block Kit card to Slack channel {channel_id}...")

        startup_name  = copy.get("startup_name", "Our Startup")
        tagline       = copy.get("tagline", "")
        short_desc    = copy.get("short_description", "")
        twitter_post  = copy.get("social_posts", {}).get("twitter", "")
        pr_link       = f"<{pr_url}|View PR>" if pr_url else "_(no PR yet)_"

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"New Launch: {startup_name}", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{tagline}*"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": short_desc},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*GitHub PR:*\n{pr_link}"},
                    {"type": "mrkdwn", "text": "*Status:*\nReady for review"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Twitter copy:*\n{twitter_post}"},
            },
        ]

        payload = {
            "channel": channel_id,
            "text":    f"New Launch: {startup_name} — {tagline}",
            "blocks":  blocks,
        }

        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {slack_token}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=15,
            )
            data = resp.json()
            if data.get("ok"):
                print(f"[MARKETING] Slack Block Kit message posted (ts={data.get('ts')}) ✓")
            else:
                print(f"[MARKETING] Slack error: {data.get('error', 'unknown')}")
        except Exception as exc:
            print(f"[MARKETING] Slack request error: {exc}")
