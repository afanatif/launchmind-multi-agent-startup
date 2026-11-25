# agents/engineer_agent.py
#
# PHASE 2 — Full GitHub integration enabled.
# Steps: generate HTML → GitHub issue → branch → commit → PR → report to CEO.

import base64
import json
import os
import time
from pathlib import Path

import requests
from google import genai

from message_bus import send_message, receive_messages

OUTPUT_HTML = "landing_page.html"
BRANCH_NAME = "agent-landing-page"
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "landing_page"


class EngineerAgent:
    """
    Generates an HTML landing page and pushes it to GitHub via the REST API.

    Steps
    -----
    1. generate_landing_page  – Gemini writes a complete HTML file
    2. save to landing_page.html locally
    3. create_github_issue    – opens a tracking issue on GitHub
    4. create_branch          – creates 'agent-landing-page' off main
    5. commit_file            – base64-encodes & commits index.html
    6. open_pull_request      – opens a PR with AI-written description
    7. Reports pr_url + issue_url back to CEO via the message bus
    """

    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        self.agent_name = "engineer"
        self._github_headers = {
            "Authorization": f"token {os.environ.get('GITHUB_TOKEN', '')}",
            "Accept": "application/vnd.github+json",
        }
        self._repo = os.environ.get("GITHUB_REPO", "")  # e.g. "username/launchmind"

    # ------------------------------------------------------------------
    # Internal Gemini helper
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
                print(f"[ENGINEER] Gemini API error (attempt {attempt + 1}): {exc}")
                if (
                    "429" in str(exc)
                    or "RESOURCE_EXHAUSTED" in str(exc)
                    or "503" in str(exc)
                    or "UNAVAILABLE" in str(exc)
                ):
                    print("[ENGINEER] Gemini unavailable/quota hit. 🔄 Falling back to Hugging Face...")
                    return self._call_hf(system_prompt, user_prompt)

                if attempt < retries:
                    print("[ENGINEER] Retrying Gemini call...")
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

    def run(self) -> None:
        messages = receive_messages(self.agent_name)
        if not messages:
            print("[ENGINEER] No messages in inbox.")
            return

        for msg in messages:
            if msg["message_type"] == "task":
                print("[ENGINEER] Received task from CEO.")
                self.build(msg["payload"], msg["message_id"])
            else:
                print(f"[ENGINEER] Unknown message type: {msg['message_type']}. Skipping.")

    # ------------------------------------------------------------------
    # build
    # ------------------------------------------------------------------

    def build(self, payload: dict, parent_id: str) -> None:
        spec = payload.get("spec", {})

        # Step 1: Generate landing page HTML via Gemini
        html_content = self.generate_landing_page(spec)

        # Step 2: Save locally so the user can always view it
        output_path = os.path.join(os.getcwd(), OUTPUT_HTML)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"[ENGINEER] ✅  Landing page saved to: {output_path}")

        pr_url = ""
        issue_url = ""

        if self._repo and os.environ.get("GITHUB_TOKEN"):
            # Step 3: GitHub issue
            issue_url = self.create_github_issue(spec)

            # Step 4: Create branch
            self.create_branch()

            # Step 5: Commit the HTML file
            self.commit_file(html_content)

            # Step 6: Open PR
            pr_url = self.open_pull_request(spec)
        else:
            print("[ENGINEER] ⚠️  GITHUB_TOKEN or GITHUB_REPO not set — skipping GitHub steps.")

        # Step 7: Report back to CEO
        send_message(
            from_agent=self.agent_name,
            to_agent="ceo",
            message_type="result",
            payload={
                "pr_url": pr_url,
                "issue_url": issue_url,
                "html_saved_to": output_path,
            },
            parent_id=parent_id,
        )

    # ------------------------------------------------------------------
    # generate_landing_page
    # ------------------------------------------------------------------

    def generate_landing_page(self, spec: dict) -> str:
        print("[ENGINEER] Building landing page from polished UI template...")

        template_html = self._choose_template(spec)
        seeded_html = self._seed_template(spec, template_html)

        system_prompt = (
            "You are a senior frontend developer. You write clean, modern, responsive HTML "
            "and CSS. Improve an existing landing page template into a polished startup page. "
            "Keep it professional, trustworthy, and modern. Avoid creepy visuals or gimmicky copy. "
            "Do not use external CSS frameworks. Keep everything in one self-contained HTML file."
        )

        user_prompt = (
            f"Startup product context:\n"
            f"Value proposition: {spec.get('value_proposition', '')}\n"
            f"Features: {json.dumps(spec.get('features', []))}\n"
            f"User personas: {json.dumps(spec.get('personas', []))}\n\n"
            "You are given a base HTML template. Refine and customize this template while preserving a high-quality design system feel.\n"
            "Requirements:\n"
            "- Keep semantic sections and responsive design.\n"
            "- Keep clean spacing, readable contrast, and modern typography.\n"
            "- Include headline, subheadline, features, personas/target users, CTA button.\n"
            "- Mention at least 4 product features clearly.\n"
            "- Keep output professional and realistic for startup demo.\n\n"
            f"Base template HTML:\n{seeded_html}\n\n"
            "Return ONLY the complete HTML code starting with <!DOCTYPE html>. "
            "Nothing else. No explanation. No markdown."
        )

        html = self._call_gemini(system_prompt, user_prompt)

        # Strip any accidental markdown fences Gemini may add
        if html.strip().startswith("```"):
            lines = html.strip().splitlines()
            html = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        print("[ENGINEER] HTML generated ✓")
        return html

    def _choose_template(self, spec: dict) -> str:
        value_prop = (spec.get("value_proposition", "") or "").lower()
        feature_blob = " ".join(
            f"{f.get('name', '')} {f.get('description', '')}" for f in spec.get("features", [])
        ).lower()
        text = f"{value_prop} {feature_blob}"

        template_name = "tech.html"
        if any(k in text for k in ["food", "meal", "kitchen", "restaurant", "chef", "delivery"]):
            template_name = "food.html"
        elif any(k in text for k in ["learn", "student", "course", "exam", "school", "education"]):
            template_name = "education.html"
        elif any(k in text for k in ["finance", "invoice", "budget", "payment", "accounting", "money"]):
            template_name = "finance.html"

        template_path = TEMPLATES_DIR / template_name
        print(f"[ENGINEER] Selected UI template: {template_name}")
        try:
            return template_path.read_text(encoding="utf-8")
        except Exception:
            # Safe fallback if template file is missing for any reason
            return (TEMPLATES_DIR / "tech.html").read_text(encoding="utf-8")

    def _seed_template(self, spec: dict, template_html: str) -> str:
        startup_name = self._derive_startup_name(spec)
        features = spec.get("features", [])
        personas = spec.get("personas", [])

        replacements = {
            "{{STARTUP_NAME}}": startup_name,
            "{{HEADLINE}}": spec.get("value_proposition", "Build something people love"),
            "{{SUBHEADLINE}}": self._safe_subheadline(personas),
            "{{FEATURE_1_TITLE}}": self._feature_field(features, 0, "name", "Core Feature One"),
            "{{FEATURE_1_DESC}}": self._feature_field(features, 0, "description", "High-impact capability."),
            "{{FEATURE_2_TITLE}}": self._feature_field(features, 1, "name", "Core Feature Two"),
            "{{FEATURE_2_DESC}}": self._feature_field(features, 1, "description", "Practical and user-friendly."),
            "{{FEATURE_3_TITLE}}": self._feature_field(features, 2, "name", "Core Feature Three"),
            "{{FEATURE_3_DESC}}": self._feature_field(features, 2, "description", "Built for daily reliability."),
            "{{FEATURE_4_TITLE}}": self._feature_field(features, 3, "name", "Core Feature Four"),
            "{{FEATURE_4_DESC}}": self._feature_field(features, 3, "description", "Designed to scale smoothly."),
        }

        seeded = template_html
        for k, v in replacements.items():
            seeded = seeded.replace(k, str(v))
        return seeded

    def _derive_startup_name(self, spec: dict) -> str:
        value = (spec.get("value_proposition", "") or "").strip()
        if not value:
            return "LaunchMind Startup"
        first = value.split()[0]
        return first[:1].upper() + first[1:20]

    def _safe_subheadline(self, personas: list) -> str:
        if personas:
            role = personas[0].get("role", "modern users")
            pain = personas[0].get("pain_point", "save time and effort")
            return f"Built for {role} who need to {pain}."
        return "A focused product experience that solves a real user problem."

    def _feature_field(self, features: list, idx: int, field: str, fallback: str) -> str:
        if idx < len(features):
            val = features[idx].get(field, "")
            if val:
                return str(val)
        return fallback

    # ------------------------------------------------------------------
    # create_github_issue
    # ------------------------------------------------------------------

    def create_github_issue(self, spec: dict) -> str:
        print("[ENGINEER] Creating GitHub issue...")

        system_prompt = "You are a software engineer writing a GitHub issue. Be technical and specific."
        user_prompt = (
            f"Write a GitHub issue for building a landing page for this startup:\n"
            f"{spec.get('value_proposition', '')}\n"
            f"Features to include: {json.dumps(spec.get('features', []))}\n"
            "Return ONLY a valid JSON object with no extra text, no markdown, no backticks:\n"
            '{"title": "string", "body": "string"}'
        )

        raw = self._call_gemini(system_prompt, user_prompt)

        try:
            issue_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[ENGINEER] JSON parse error for issue: {exc}\nRaw: {raw}")
            issue_data = {
                "title": "Build landing page for new startup",
                "body": f"**Value proposition:** {spec.get('value_proposition', '')}\n\n**Task:** Build the landing page as generated by EngineerAgent.",
            }

        url = f"https://api.github.com/repos/{self._repo}/issues"
        resp = requests.post(url, json=issue_data, headers=self._github_headers, timeout=15)

        if resp.status_code == 201:
            issue_url = resp.json().get("html_url", "")
            print(f"[ENGINEER] ✅  GitHub issue created: {issue_url}")
            return issue_url
        else:
            print(f"[ENGINEER] ❌  Failed to create issue ({resp.status_code}): {resp.text}")
            return ""

    # ------------------------------------------------------------------
    # create_branch
    # ------------------------------------------------------------------

    def create_branch(self) -> None:
        print(f"[ENGINEER] Creating branch '{BRANCH_NAME}'...")

        # Get SHA of main
        ref_url = f"https://api.github.com/repos/{self._repo}/git/ref/heads/main"
        resp = requests.get(ref_url, headers=self._github_headers, timeout=15)

        if resp.status_code != 200:
            print(f"[ENGINEER] ❌  Could not get main SHA ({resp.status_code}): {resp.text}")
            return

        main_sha = resp.json()["object"]["sha"]

        # Delete branch if it already exists (idempotent re-runs)
        del_url = f"https://api.github.com/repos/{self._repo}/git/refs/heads/{BRANCH_NAME}"
        del_resp = requests.delete(del_url, headers=self._github_headers, timeout=15)
        if del_resp.status_code == 204:
            print(f"[ENGINEER] Old branch '{BRANCH_NAME}' removed (clean slate).")

        # Create new branch
        create_url = f"https://api.github.com/repos/{self._repo}/git/refs"
        payload = {"ref": f"refs/heads/{BRANCH_NAME}", "sha": main_sha}
        resp2 = requests.post(create_url, json=payload, headers=self._github_headers, timeout=15)

        if resp2.status_code == 201:
            print(f"[ENGINEER] ✅  Branch '{BRANCH_NAME}' created.")
        else:
            print(f"[ENGINEER] ❌  Failed to create branch ({resp2.status_code}): {resp2.text}")

    # ------------------------------------------------------------------
    # commit_file
    # ------------------------------------------------------------------

    def commit_file(self, html_content: str) -> None:
        print("[ENGINEER] Committing index.html to GitHub...")

        encoded = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")

        # Check if file already exists on the branch (need SHA to update)
        file_url = f"https://api.github.com/repos/{self._repo}/contents/index.html"
        params = {"ref": BRANCH_NAME}
        existing = requests.get(file_url, params=params, headers=self._github_headers, timeout=15)

        commit_payload: dict = {
            "message": "Add landing page — authored by EngineerAgent <agent@launchmind.ai>",
            "content": encoded,
            "branch": BRANCH_NAME,
        }
        if existing.status_code == 200:
            commit_payload["sha"] = existing.json().get("sha", "")

        resp = requests.put(file_url, json=commit_payload, headers=self._github_headers, timeout=15)

        if resp.status_code in (200, 201):
            print("[ENGINEER] ✅  index.html committed to branch.")
        else:
            print(f"[ENGINEER] ❌  Commit failed ({resp.status_code}): {resp.text}")

    # ------------------------------------------------------------------
    # open_pull_request
    # ------------------------------------------------------------------

    def open_pull_request(self, spec: dict) -> str:
        print("[ENGINEER] Opening GitHub pull request...")

        system_prompt = (
            "You are a software engineer writing a GitHub pull request description. "
            "Be clear and professional."
        )
        user_prompt = (
            f"Write a pull request title and body for a landing page built for:\n"
            f"{spec.get('value_proposition', '')}\n"
            "Return ONLY a valid JSON object with no extra text, no markdown, no backticks:\n"
            '{"title": "string", "body": "string"}'
        )

        raw = self._call_gemini(system_prompt, user_prompt)

        try:
            pr_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[ENGINEER] JSON parse error for PR: {exc}\nRaw: {raw}")
            pr_data = {
                "title": "Add AI-generated landing page",
                "body": f"This PR adds the landing page generated by EngineerAgent.\n\n**Value proposition:** {spec.get('value_proposition', '')}",
            }

        url = f"https://api.github.com/repos/{self._repo}/pulls"
        payload = {
            "title": pr_data.get("title", "Add landing page"),
            "body": pr_data.get("body", ""),
            "head": BRANCH_NAME,
            "base": "main",
        }
        resp = requests.post(url, json=payload, headers=self._github_headers, timeout=15)

        if resp.status_code == 201:
            pr_url = resp.json().get("html_url", "")
            print(f"[ENGINEER] ✅  Pull request opened: {pr_url}")
            return pr_url
        else:
            print(f"[ENGINEER] ❌  Failed to open PR ({resp.status_code}): {resp.text}")
            return ""
