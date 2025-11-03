import os
import time
import random
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI


MAX_TOKENS = 8192


def _is_anthropic_model_name(name: str) -> bool:
    if not name:
        return False
    n = str(name).lower()
    return ("claude" in n) or ("anthropic" in n) or ("sonnet" in n)

def _strip_cache_control_from_messages(messages):
    if not isinstance(messages, list):
        return messages
    msgs = deepcopy(messages)
    for m in msgs:
        # drop message-level cache_control if any
        if isinstance(m, dict):
            m.pop("cache_control", None)
            c = m.get("content")
            # content could be dict or list of dicts depending on adapters
            if isinstance(c, dict):
                c.pop("cache_control", None)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict):
                        blk.pop("cache_control", None)
    return msgs


class Model:
    def __init__(self, model_name, system_prompt = None):
        self.model_name = model_name
        if "gpt-5" == model_name:
            self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        else:
            self.client = OpenAI(
                api_key=os.getenv("LLM_API_KEY"),
                base_url=os.getenv("LLM_BASE_URL"),
            )

        self.system_prompt = system_prompt if system_prompt is not None else "You are a helpful assistant."
        self.reset()

    def reset(self):
        self.message_history = [dict(role="system", content=self.system_prompt)]
        self.history = []

    @property
    def last_output_text(self):
        return self.history[-1]["output_text"]

    @property
    def config(self):
        return dict(model_name=self.model_name, system_prompt=self.system_prompt)

    def _extract_output_text_from_responses(self, resp):
        try:
            t = getattr(resp, "output_text", None)
            if t:
                return t
        except Exception:
            pass
        try:
            data = resp.model_dump()
        except Exception:
            try:
                data = resp.__dict__
            except Exception:
                data = None
        if isinstance(data, dict):
            out = data.get("output") or []
            parts = []
            for item in out:
                content = item.get("content") if isinstance(item, dict) else None
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict):
                            txt = c.get("text") or c.get("output_text") or c.get("content")
                            if txt:
                                parts.append(txt)
            if parts:
                return "".join(parts)
        return ""

    def _call_one(self, messages, max_retries = 3, backoff = 1.5, **kwargs):
        for attempt in range(max_retries):
            try:
                if "gpt-5" == self.model_name:
                    resp = self.client.responses.create(
                        model=self.model_name,
                        input=messages,
                        **{k: v for k, v in kwargs.items() if k not in ("max_tokens",)},
                        reasoning=dict(effort="minimal"),
                    )
                    
                    return self._extract_output_text_from_responses(resp)
                else:
                    send_messages = messages
                    if _is_anthropic_model_name(self.model_name):
                        send_messages = _strip_cache_control_from_messages(send_messages)
                    resp = self.client.chat.completions.create(
                        model=self.model_name,
                        messages=send_messages,
                        max_tokens=kwargs.get("max_tokens", MAX_TOKENS),
                    )
                    return resp.choices[0].message.content or ""
            except Exception:
                if attempt == max_retries - 1:
                    return ""
                sleep_s = (backoff ** attempt) + random.random() * 0.25
                time.sleep(sleep_s)
                
    def _apply_cache_control(self, messages):
        out_messages = []
        for m in messages:
            block = dict(m)
            content = block.get('content')
            if isinstance(content, str) and len(content) > 0:
                block['cache_control'] = {'type': 'ephemeral'}
            out_messages.append(block)
        return out_messages
    
    def _to_oai_tools(self, tools):
        if not tools:
            return None
        out_tools = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                out_tools.append(t)
            else:
                out_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name"),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    }
                })
        return out_tools
    
    def _parse_oai_toolcalls(self, resp):
        text = ""
        tool_calls = []
        choices = getattr(resp, "choices", None) or (resp.get("choices", []) if isinstance(resp, dict) else None)
        if not choices:
            return text, tool_calls
        ch = choices[0]
        msg = getattr(ch, "message", None) or (ch.get("message") if isinstance(ch, dict) else None)
        if not msg:
            return text, tool_calls
        if getattr(msg, "content", None):
            text = msg.content or ""
        elif isinstance(msg, dict) and "content" in msg:
            # content could be a str in some client versions
            text = msg.get("content") or ""

        tc_list = getattr(msg, "tool_calls", None) or (msg.get("tool_calls", []) if isinstance(msg, dict) else [])
        for tc in tc_list:
            tc_type = getattr(tc, "type", None) or (tc.get("type") if isinstance(tc, dict) else None)
            if tc_type == "function":
                fn = getattr(tc, "function", None) or (tc.get("function") if isinstance(tc, dict) else None)
                if not fn:
                    continue
                args = getattr(fn, "arguments", None) or (fn.get("arguments") if isinstance(fn, dict) else None)
                args_str = args if isinstance(args, str) else json.dumps(args or {})
                tool_calls.append({
                    "id": getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None),
                    "type": "function",
                    "function": {
                        "name": getattr(fn, "name", "") or (fn.get("name") if isinstance(fn, dict) else ""),
                        "arguments": args_str,
                    }
                })
        return text, tool_calls

    def generate(self, input_text, **kwargs):
        self.message_history.append(dict(role="user", content=input_text))
        if "gpt-5" == self.model_name:
            response = self.client.responses.create(
                model=self.model_name,
                input=self.message_history,
                reasoning=dict(effort="minimal"),
                text=dict(verbosity="low"),
            )
            output_content = response.output[-1].content[0]
            output_text = output_content.text
        else:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=self.message_history,
                max_tokens=MAX_TOKENS,
            )
            output_text = response.choices[0].message.content or ""
        self.message_history.append(dict(role="assistant", content=output_text))
        try:
            completion = response.model_dump()
        except Exception:
            completion = {}

        self.history.append(
            dict(
                input_text=input_text,
                output_text=output_text,
                completion=completion,
            )
        )

        return dict(input_text=input_text, output_text=output_text)

    def generate_with_messages(self, messages_or_batches, parallelism = 8, **kwargs):
        is_batch = (
            isinstance(messages_or_batches, list)
            and len(messages_or_batches) > 0
            and isinstance(messages_or_batches[0], list)
            and (len(messages_or_batches[0]) == 0 or isinstance(messages_or_batches[0][0], dict))
        )
        if not is_batch:
            return self._call_one(messages_or_batches, **kwargs)

        batches = messages_or_batches
        n = len(batches)
        results = [None] * n

        def worker(idx, msgs):
            text = self._call_one(msgs, **kwargs)
            return idx, text

        max_workers = max(1, min(int(parallelism), n))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(worker, i, m) for i, m in enumerate(batches)]
            for fut in as_completed(futures):
                try:
                    i, txt = fut.result()
                except Exception:
                    i, txt = -1, ""
                if 0 <= i < n:
                    results[i] = txt if txt is not None else ""

        return results

    def embedding(self, text, model_name = None, normalize = True, max_retries = 3, backoff = 1.5):
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        text = text.strip()
        if not text:
            return {"embedding": [], "model": model_name or "", "dim": 0}

        embed_model = "azure/text-embedding-3-large"
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = self.client.embeddings.create(model=embed_model, input=text)
                vec = resp.data[0].embedding
                if normalize:
                    s = sum(v * v for v in vec)
                    if s > 0:
                        r = s ** 0.5
                        if r != 0.0:
                            vec = [v / r for v in vec]
                return {"embedding": vec, "model": embed_model, "dim": len(vec)}
            except Exception as e:
                last_err = e
                print(f"Error in embedding: {e}")
                if attempt == max_retries - 1:
                    return {"embedding": [], "model": embed_model, "dim": 0}
                sleep_s = (backoff ** attempt) + random.random() * 0.25
                time.sleep(sleep_s)
                
    def generate_with_tools(self, messages, tools, previous_response_id=None, max_tokens=MAX_TOKENS, **kwargs):
        if "gpt-5" == self.model_name:
            kwargs = {
                "model": self.model_name,
                "input": messages,
                "max_output_tokens": max_tokens,
            }
            if tools:
                kwargs["tools"] = tools
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            return self.client.responses.create(**kwargs)
        else:
            messages = self._apply_cache_control(messages)
            if _is_anthropic_model_name(self.model_name):
                messages = _strip_cache_control_from_messages(messages)
            oai_tools = self._to_oai_tools(tools)
            kwargs = {
                "model": self.model_name,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if oai_tools:
                kwargs["tools"] = oai_tools
                kwargs["tool_choice"] = "auto"
            resp = self.client.chat.completions.create(**kwargs)
            text, tool_calls = self._parse_oai_toolcalls(resp)
            normalized_output = []
            if text:
                normalized_output.append({
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tc in tool_calls:
                normalized_output.append({
                    "type": "function_call",
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                    "call_id": tc["id"],
                    "status": "completed",
                })

            return {
                "id": getattr(resp, "id", None) or (resp.get("id") if isinstance(resp, dict) else None),
                "output": normalized_output,
                "_raw_chat_completion": resp,
            }
