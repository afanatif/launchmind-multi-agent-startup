from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests
from google import genai

from message_bus import receive_messages, send_message


class QAAgent:
    """
    Reviews Engineer and Marketing outputs, posts inline PR comments, and sends
    a structured pass/fail report back to the CEO.
    """

    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        self.agent_name = "qa"
        self._repo = os.environ.get("GITHUB_REPO", "")
        self._github_headers = {
            "Authorization": f"token {os.environ.get('GITHUB_TOKEN', '')}",
            "Accept": "application/vnd.github+json",
        }

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
                print(f"[QA] Gemini API error (attempt {attempt + 1}): {exc}")
                if (
                    "429" in str(exc)
                    or "RESOURCE_EXHAUSTED" in str(exc)
                    or "503" in str(exc)
                    or "UNAVAILABLE" in str(exc)
                ):
                    print("[QA] Gemini unavailable/quota hit. Falling back to Hugging Face...")
                    return self._call_hf(system_prompt, user_prompt)
                if attempt < retries:
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
            max_tokens=4000,
        )
        return response.choices[0].message.content or ""

    def run(self) -> None:
        messages = receive_messages(self.agent_name)
        if not messages:
            print("[QA] No messages in inbox.")
            return

        for msg in messages:
            if msg["message_type"] == "task":
                print("[QA] Received QA task from CEO.")
                self.review(msg["payload"], msg["message_id"])
            else:
                print(f"[QA] Unknown message type: {msg['message_type']}. Skipping.")

    def review(self, payload: dict, parent_id: str) -> None:
        spec = payload.get("spec", {})
        marketing_copy = payload.get("marketing_copy", {})
        pr_url = payload.get("pr_url", "")
        html_path = payload.get("html_path", "")

        html_content = self._read_html_content(html_path)
        qa_report = self._review_with_llm(spec, marketing_copy, html_content)

        comments_posted = 0
        posted_comment_bodies: list[str] = []
        if pr_url and self._repo and os.environ.get("GITHUB_TOKEN"):
            comments_posted, posted_comment_bodies = self._post_inline_pr_comments(
                pr_url=pr_url,
                inline_comments=qa_report.get("inline_comments", []),
            )
        else:
            print("[QA] Missing PR URL or GitHub credentials; skipping PR comments.")

        send_message(
            from_agent=self.agent_name,
            to_agent="ceo",
            message_type="result",
            payload={
                "verdict": qa_report.get("verdict", "fail"),
                "issues": qa_report.get("issues", []),
                "comments_posted": comments_posted,
                "comment_bodies": posted_comment_bodies,
                "pr_url": pr_url,
            },
            parent_id=parent_id,
        )

    def _read_html_content(self, html_path: str) -> str:
        if not html_path:
            return ""
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as exc:
            print(f"[QA] Could not read HTML file '{html_path}': {exc}")
            return ""

    def _review_with_llm(self, spec: dict, marketing_copy: dict, html_content: str) -> dict[str, Any]:
        print("[QA] Reviewing HTML + marketing copy via Gemini...")

        system_prompt = (
            "You are a strict QA reviewer for a startup launch pipeline. "
            "You review landing page HTML against product spec and review marketing copy quality. "
            "Be specific, concise, and practical."
        )

        html_excerpt = html_content[:8000]
        user_prompt = (
            "Review the following artifacts and return ONLY valid JSON.\n\n"
            f"Product spec:\n{json.dumps(spec)}\n\n"
            f"Marketing copy:\n{json.dumps(marketing_copy)}\n\n"
            f"Landing page HTML:\n{html_excerpt}\n\n"
            "Return JSON in this exact shape:\n"
            "{\n"
            '  "verdict": "pass" or "fail",\n'
            '  "issues": ["issue 1", "issue 2"],\n'
            '  "inline_comments": [\n'
            '    {"line": 10, "body": "specific HTML review comment"},\n'
            '    {"line": 30, "body": "specific HTML review comment"}\n'
            "  ]\n"
            "}\n"
            "Rules:\n"
            "- Provide at least 2 inline_comments focused on HTML quality and spec alignment.\n"
            "- Use realistic line numbers from the HTML.\n"
            "- Mark verdict fail if major mismatch exists."
        )

        raw = self._call_gemini(system_prompt, user_prompt)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[QA] JSON parse failed for review output. Raw:\n{raw}")
            parsed = {
                "verdict": "fail",
                "issues": [
                    "QA model returned malformed JSON; manual review required.",
                    "Unable to confidently verify HTML and marketing alignment.",
                ],
                "inline_comments": [
                    {"line": 1, "body": "QA fallback: verify document structure and semantic sections."},
                    {"line": 5, "body": "QA fallback: verify headline and core features match product spec."},
                ],
            }

        if not isinstance(parsed.get("issues"), list):
            parsed["issues"] = ["Invalid issues format in QA output."]
        if not isinstance(parsed.get("inline_comments"), list):
            parsed["inline_comments"] = []
        if parsed.get("verdict") not in ("pass", "fail"):
            parsed["verdict"] = "fail"
        return parsed

    def _extract_pr_number(self, pr_url: str) -> int | None:
        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            return None
        return int(match.group(1))

    def _post_inline_pr_comments(self, pr_url: str, inline_comments: list[dict]) -> tuple[int, list[str]]:
        pr_number = self._extract_pr_number(pr_url)
        if pr_number is None:
            print(f"[QA] Could not parse PR number from URL: {pr_url}")
            return 0, []

        pr_api = f"https://api.github.com/repos/{self._repo}/pulls/{pr_number}"
        pr_resp = requests.get(pr_api, headers=self._github_headers, timeout=20)
        if pr_resp.status_code != 200:
            print(f"[QA] Failed to fetch PR details ({pr_resp.status_code}): {pr_resp.text}")
            return 0, []

        pr_data = pr_resp.json()
        commit_id = pr_data.get("head", {}).get("sha", "")
        if not commit_id:
            print("[QA] No head commit SHA on PR; cannot post inline comments.")
            return 0, []

        files_api = f"https://api.github.com/repos/{self._repo}/pulls/{pr_number}/files"
        files_resp = requests.get(files_api, headers=self._github_headers, timeout=20)
        if files_resp.status_code != 200:
            print(f"[QA] Failed to fetch PR files ({files_resp.status_code}): {files_resp.text}")
            return 0, []

        path = "index.html"
        available_paths = {f.get("filename", "") for f in files_resp.json()}
        if path not in available_paths and available_paths:
            # fallback to first file in the PR
            path = list(available_paths)[0]

        posted = 0
        bodies: list[str] = []
        for c in inline_comments[:5]:
            line = int(c.get("line", 1))
            body = str(c.get("body", "")).strip() or "QA review comment."
            comment_payload = {
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": max(1, line),
                "side": "RIGHT",
            }
            comment_api = f"https://api.github.com/repos/{self._repo}/pulls/{pr_number}/comments"
            resp = requests.post(
                comment_api,
                json=comment_payload,
                headers=self._github_headers,
                timeout=20,
            )
            if resp.status_code == 201:
                posted += 1
                bodies.append(body)
            else:
                print(f"[QA] Failed posting inline comment ({resp.status_code}): {resp.text}")

        print(f"[QA] Posted {posted} inline PR comment(s).")
        return posted, bodies
