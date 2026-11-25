# agents/product_agent.py

import os
import json
import time

from google import genai

from message_bus import send_message, receive_messages


class ProductAgent:
    """
    Generates (and revises) structured product specifications.

    Inputs  : "task" or "revision_request" messages from CEO
    Outputs : "result" messages back to CEO containing the spec dict
    """

    def __init__(self):
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        self.agent_name = "product"
        self._last_spec: dict = {}

    # ------------------------------------------------------------------
    # Internal Claude helper
    # ------------------------------------------------------------------

    def _call_gemini(self, system_prompt: str, user_prompt: str, retries: int = 1) -> str:
        for attempt in range(retries + 1):
            try:
                response = self.client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=user_prompt,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=system_prompt,
                    )
                )
                return response.text
            except Exception as exc:
                print(f"[PRODUCT] Gemini API error (attempt {attempt + 1}): {exc}")
                if (
                    "429" in str(exc)
                    or "RESOURCE_EXHAUSTED" in str(exc)
                    or "503" in str(exc)
                    or "UNAVAILABLE" in str(exc)
                ):
                    print("[PRODUCT] Gemini unavailable/quota hit. 🔄 Falling back to Hugging Face...")
                    return self._call_hf(system_prompt, user_prompt)

                if attempt < retries:
                    print("[PRODUCT] Retrying Gemini call...")
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
        """Drain the 'product' inbox and handle each message."""
        messages = receive_messages(self.agent_name)
        if not messages:
            print("[PRODUCT] No messages in inbox.")
            return

        for msg in messages:
            mtype = msg["message_type"]
            payload = msg["payload"]
            parent_id = msg["message_id"]

            if mtype == "task":
                print("[PRODUCT] Received task from CEO.")
                self.generate_spec(payload, parent_id)

            elif mtype == "revision_request":
                print("[PRODUCT] Received revision request from CEO.")
                self.revise_spec(payload, parent_id)

            else:
                print(f"[PRODUCT] Unknown message type: {mtype}. Skipping.")

    # ------------------------------------------------------------------
    # generate_spec
    # ------------------------------------------------------------------

    def generate_spec(self, payload: dict, parent_id: str) -> None:
        """Generate a product spec and send it back to the CEO."""
        print("[PRODUCT] Generating product specification...")

        system_prompt = (
            "You are an experienced Product Manager at a startup. Your job is to create "
            "detailed, realistic product specifications. You write specs that are specific "
            "enough for developers to build from and marketers to promote. You always think "
            "about real users with real problems. Never be vague or generic."
        )

        user_prompt = (
            f"Create a complete product specification for this startup:\n"
            f"Idea: {payload.get('idea', '')}\n"
            f"Focus: {payload.get('focus', '')}\n\n"
            "Return ONLY a valid JSON object with NO extra text, NO markdown, NO backticks:\n"
            "{\n"
            "  \"value_proposition\": \"one sentence describing what the product does and for whom\",\n"
            "  \"personas\": [\n"
            "    {\"name\": \"string\", \"role\": \"string\", \"pain_point\": \"string\"},\n"
            "    {\"name\": \"string\", \"role\": \"string\", \"pain_point\": \"string\"},\n"
            "    {\"name\": \"string\", \"role\": \"string\", \"pain_point\": \"string\"}\n"
            "  ],\n"
            "  \"features\": [\n"
            "    {\"name\": \"string\", \"description\": \"string\", \"priority\": 1},\n"
            "    {\"name\": \"string\", \"description\": \"string\", \"priority\": 2},\n"
            "    {\"name\": \"string\", \"description\": \"string\", \"priority\": 3},\n"
            "    {\"name\": \"string\", \"description\": \"string\", \"priority\": 4},\n"
            "    {\"name\": \"string\", \"description\": \"string\", \"priority\": 5}\n"
            "  ],\n"
            "  \"user_stories\": [\n"
            "    \"As a [user], I want to [action] so that [benefit]\",\n"
            "    \"As a [user], I want to [action] so that [benefit]\",\n"
            "    \"As a [user], I want to [action] so that [benefit]\"\n"
            "  ]\n"
            "}"
        )

        raw = self._call_gemini(system_prompt, user_prompt)

        try:
            spec = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[PRODUCT] JSON parse error: {exc}")
            print(f"[PRODUCT] Raw response:\n{raw}")
            spec = {"raw_response": raw}

        self._last_spec = spec
        print("[PRODUCT] Specification generated. Sending to CEO...")

        send_message(
            from_agent=self.agent_name,
            to_agent="ceo",
            message_type="result",
            payload={"spec": spec},
            parent_id=parent_id,
        )

    # ------------------------------------------------------------------
    # revise_spec
    # ------------------------------------------------------------------

    def revise_spec(self, payload: dict, parent_id: str) -> None:
        """Revise an existing spec based on CEO feedback."""
        print("[PRODUCT] Revising product specification based on CEO feedback...")

        previous_spec = payload.get("previous_spec", self._last_spec)
        feedback = payload.get("feedback", "")

        system_prompt = (
            "You are a Product Manager who has received feedback from your CEO on a "
            "product specification you wrote. You must address every point of feedback "
            "specifically and improve the spec accordingly."
        )

        user_prompt = (
            f"Here is your previous product spec:\n{json.dumps(previous_spec, indent=2)}\n\n"
            f"Here is the CEO's feedback:\n{feedback}\n\n"
            "Fix every issue mentioned. Return ONLY a valid JSON object in the exact "
            "same format as before. No extra text, no markdown, no backticks."
        )

        raw = self._call_gemini(system_prompt, user_prompt)

        try:
            spec = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[PRODUCT] JSON parse error on revision: {exc}")
            print(f"[PRODUCT] Raw response:\n{raw}")
            spec = previous_spec  # fall back to previous if parse fails

        self._last_spec = spec
        print("[PRODUCT] Revised specification ready. Sending to CEO...")

        send_message(
            from_agent=self.agent_name,
            to_agent="ceo",
            message_type="result",
            payload={"spec": spec},
            parent_id=parent_id,
        )
