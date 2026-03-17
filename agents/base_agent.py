"""
Base agent -- Bedrock API calls with truncation detection and JSON continuation.

KEY FIX: For large documents (50+ pages), Claude's response often exceeds
max_tokens and gets truncated mid-JSON. This causes parse failure and empty
results flowing to downstream stages. This version:

  1. Detects truncation via stop_reason == "end_turn" vs "max_tokens"
  2. Automatically continues truncated responses with follow-up calls
  3. Repairs truncated JSON by finding the last complete item in arrays
"""
import json
import logging
import re
import time
from typing import Any, Optional

from anthropic import AnthropicBedrock

from config.settings import settings
from utils.artifact_tracker import ArtifactTracker
import httpx
logger = logging.getLogger(__name__)


class BaseAgent:
    def __init__(self, tracker: ArtifactTracker, stage_name: str):
        self.tracker = tracker
        self.stage_name = stage_name
        _BEDROCK_TIMEOUT = httpx.Timeout(
                     timeout=1800.0,   # 30 min (Opus on large docs can be slow)
                     connect=30.0,     # 30s to establish connection
                        )
        self.client = AnthropicBedrock(aws_region=settings.aws.region,
                                       timeout=_BEDROCK_TIMEOUT,)
        self.api_call_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def call_bedrock(
        self,
        system_prompt: str,
        user_content: list[dict],
        max_tokens: Optional[int] = None,
        temperature: float = 0.1,
    ) -> dict:
        """Call Claude via Bedrock. Returns text + stop_reason for truncation check."""
        max_tokens = max_tokens or settings.aws.max_tokens
        last_err = None

        for attempt in range(1, settings.aws.max_retries + 1):
            try:
                resp = self.client.messages.create(
                    model=settings.aws.model_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                )

                self.api_call_count += 1
                inp, out = resp.usage.input_tokens, resp.usage.output_tokens
                self.total_input_tokens += inp
                self.total_output_tokens += out
                text = "".join(b.text for b in resp.content if hasattr(b, "text"))
                stop = resp.stop_reason  # "end_turn" = complete, "max_tokens" = truncated

                logger.debug(
                    f"[{self.stage_name}] API #{self.api_call_count}: "
                    f"{inp}in/{out}out stop={stop}"
                )

                if stop == "max_tokens":
                    logger.warning(
                        f"[{self.stage_name}] Response TRUNCATED at {out} tokens "
                        f"(limit: {max_tokens})"
                    )

                return {
                    "text": text,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "stop_reason": stop,
                    "truncated": stop == "max_tokens",
                }

            except Exception as e:
                last_err = e
                wait = settings.aws.base_backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    f"[{self.stage_name}] Attempt {attempt}/{settings.aws.max_retries} "
                    f"failed: {type(e).__name__}: {e}. Retry in {wait:.1f}s"
                )
                if attempt < settings.aws.max_retries:
                    time.sleep(wait)

        msg = (
            f"API failed after {settings.aws.max_retries} attempts: {last_err}\n"
            f"Check AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN."
        )
        self.tracker.log_error(self.stage_name, "api_error", msg)
        raise RuntimeError(msg)

    def call_bedrock_with_continuation(
        self,
        system_prompt: str,
        user_content: list[dict],
        max_tokens: Optional[int] = None,
        temperature: float = 0.1,
    ) -> dict:
        """Call Claude and automatically continue if response is truncated.

        Uses multi-turn conversation to ask Claude to continue its JSON from
        where it left off, then concatenates all parts.
        """
        max_tokens = max_tokens or settings.aws.max_tokens_large

        # First call
        result = self.call_bedrock(system_prompt, user_content, max_tokens, temperature)
        accumulated_text = result["text"]

        if not result["truncated"]:
            return result

        # Response was truncated -- continue with multi-turn
        messages = [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": accumulated_text},
        ]

        for continuation in range(1, settings.aws.max_continuations + 1):
            logger.info(
                f"[{self.stage_name}] Continuation {continuation}/"
                f"{settings.aws.max_continuations}..."
            )

            # Ask to continue from where it stopped
            messages.append({
                "role": "user",
                "content": [{"type": "text", "text":
                    "Your response was truncated. Continue the JSON output EXACTLY "
                    "from where you left off. Do NOT repeat any content. Do NOT add "
                    "any preamble or explanation. Just continue the JSON."
                }],
            })

            last_err = None
            for attempt in range(1, settings.aws.max_retries + 1):
                try:
                    resp = self.client.messages.create(
                        model=settings.aws.model_id,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        system=system_prompt,
                        messages=messages,
                    )
                    self.api_call_count += 1
                    inp, out = resp.usage.input_tokens, resp.usage.output_tokens
                    self.total_input_tokens += inp
                    self.total_output_tokens += out
                    cont_text = "".join(b.text for b in resp.content if hasattr(b, "text"))
                    stop = resp.stop_reason

                    logger.debug(
                        f"[{self.stage_name}] Continuation #{continuation}: "
                        f"{inp}in/{out}out stop={stop}"
                    )

                    accumulated_text += cont_text
                    messages.append({"role": "assistant", "content": cont_text})

                    if stop != "max_tokens":
                        logger.info(
                            f"[{self.stage_name}] Continuation complete after "
                            f"{continuation} extra call(s)"
                        )
                        return {
                            "text": accumulated_text,
                            "input_tokens": result["input_tokens"] + inp,
                            "output_tokens": result["output_tokens"] + out,
                            "stop_reason": stop,
                            "truncated": False,
                            "continuations": continuation,
                        }
                    break  # Truncated again, continue outer loop

                except Exception as e:
                    last_err = e
                    wait = settings.aws.base_backoff_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        f"[{self.stage_name}] Continuation attempt {attempt} "
                        f"failed: {e}. Retry in {wait:.1f}s"
                    )
                    if attempt < settings.aws.max_retries:
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"[{self.stage_name}] Continuation failed after retries"
                        )
                        break

        # Exhausted continuations -- return what we have
        logger.warning(
            f"[{self.stage_name}] Max continuations reached. "
            f"Total accumulated: {len(accumulated_text)} chars"
        )
        return {
            "text": accumulated_text,
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "stop_reason": "max_continuations",
            "truncated": True,
            "continuations": settings.aws.max_continuations,
        }

    def parse_json_response(self, text: str, context: str = "") -> Any:
        """Parse JSON from Claude response. Handles markdown fences and truncation.

        If the JSON is truncated (incomplete), attempts to repair by:
        1. Finding the last complete object in an array
        2. Closing the array/object structure
        """
        cleaned = text.strip()
        # Strip markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()

        # Try direct parse first
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON blocks
        for pat in [r"(\{[\s\S]*\})", r"(\[[\s\S]*\])"]:
            m = re.search(pat, cleaned)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    # Try removing trailing commas
                    fixed = re.sub(r",\s*([}\]])", r"\1", m.group(1))
                    try:
                        return json.loads(fixed)
                    except json.JSONDecodeError:
                        pass

        # JSON is likely truncated -- try to repair
        repaired = self._repair_truncated_json(cleaned)
        if repaired is not None:
            logger.info(f"[{self.stage_name}] Repaired truncated JSON ({context})")
            return repaired

        self.tracker.log_error(
            self.stage_name, "json_parse_error",
            f"Failed to parse ({context}). Length: {len(text)} chars",
            raw_response=text[:500],
        )
        return None

    def _repair_truncated_json(self, text: str) -> Any:
        """Attempt to repair truncated JSON by finding last complete array item.

        Strategy:
        1. Find the main JSON structure start ({...} or [...])
        2. If it contains an array of objects, find the last complete object
        3. Close the array and outer structure
        """
        # Find the start of the main JSON
        start = -1
        for i, ch in enumerate(text):
            if ch in ('{', '['):
                start = i
                break
        if start == -1:
            return None

        # Try to find a key like "scope_items", "cost_items", "assessment_items"
        # and extract the array value
        for key_pattern in [
            r'"scope_items"\s*:\s*\[',
            r'"cost_items"\s*:\s*\[',
            r'"assessment_items"\s*:\s*\[',
            r'"confirmed_items"\s*:\s*\[',
            r'"items"\s*:\s*\[',
        ]:
            m = re.search(key_pattern, text)
            if m:
                arr_start = m.end() - 1  # Position of '['
                return self._extract_complete_array_items(text, arr_start, m.group())

        # If it starts with '[', it's a direct array
        if text[start] == '[':
            return self._extract_complete_array_items(text, start, "array")

        return None

    def _extract_complete_array_items(self, text: str, arr_start: int, context: str) -> Any:
        """Extract complete objects from a possibly-truncated JSON array.

        Walks through the text finding complete {...} objects by tracking
        brace depth. Returns all complete objects found.
        """
        items = []
        i = arr_start + 1  # Skip the '['
        length = len(text)

        while i < length:
            # Skip whitespace and commas
            while i < length and text[i] in (' ', '\n', '\r', '\t', ','):
                i += 1

            if i >= length or text[i] == ']':
                break

            if text[i] == '{':
                # Track brace depth to find complete object
                depth = 0
                obj_start = i
                in_string = False
                escape_next = False

                while i < length:
                    ch = text[i]
                    if escape_next:
                        escape_next = False
                        i += 1
                        continue
                    if ch == '\\' and in_string:
                        escape_next = True
                        i += 1
                        continue
                    if ch == '"' and not escape_next:
                        in_string = not in_string
                    elif not in_string:
                        if ch == '{':
                            depth += 1
                        elif ch == '}':
                            depth -= 1
                            if depth == 0:
                                # Complete object found
                                obj_text = text[obj_start:i + 1]
                                try:
                                    obj = json.loads(obj_text)
                                    items.append(obj)
                                except json.JSONDecodeError:
                                    pass
                                i += 1
                                break
                    i += 1
                else:
                    # Reached end without closing -- object is truncated
                    break
            else:
                i += 1

        if not items:
            return None

        logger.info(
            f"[{self.stage_name}] Recovered {len(items)} complete items "
            f"from truncated JSON ({context})"
        )

        # Try to reconstruct the original structure
        # Check if there was an outer key wrapper
        for key in ["scope_items", "cost_items", "assessment_items",
                     "confirmed_items", "items"]:
            if f'"{key}"' in text[:arr_start + 50]:
                return {key: items}

        return items

    def build_image_content(self, b64: str, mt: str = "image/jpeg") -> dict:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mt, "data": b64},
        }

    def build_text_content(self, text: str) -> dict:
        return {"type": "text", "text": text}
