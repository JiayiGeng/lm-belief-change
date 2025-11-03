import re
import json
import inspect
import traceback
from copy import deepcopy


class AgentRuntime:
    def __init__(self, tool_impl, terminal_names=None, max_tokens=8192):
        self.tool_impl = tool_impl
        self.max_tokens = max_tokens
        self.terminal_names = set(terminal_names or [])

    @staticmethod
    def get(item, key, default=None):
        if hasattr(item, key):
            return getattr(item, key, default)
        if isinstance(item, dict):
            return item.get(key, default)
        return default

    @staticmethod
    def collect_output_text(output_items):
        parts = []
        for item in output_items or []:
            if AgentRuntime.get(item, "type") == "message":
                content = AgentRuntime.get(item, "content", []) or []
                for c in content:
                    if AgentRuntime.get(c, "type") == "output_text":
                        t = AgentRuntime.get(c, "text") or ""
                        if t:
                            parts.append(t)
        return "\n".join(parts).strip()

    @staticmethod
    def coerce_json_block(raw_text):
        if not raw_text:
            return ""
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return ""
        candidate = match.group(0)
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        candidate = candidate.replace("'", '"')
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return ""

    @staticmethod
    def introspect_tool_signatures(tool_impl):
        sigs = {}
        for name, member in inspect.getmembers(tool_impl, predicate=inspect.ismethod):
            if name.startswith("_"):
                continue
            try:
                sig = inspect.signature(member)
            except Exception:
                continue
            params = []
            required = []
            var_kw = False
            for p in sig.parameters.values():
                if p.name == "self":
                    continue
                if p.kind in (inspect.Parameter.VAR_KEYWORD,):
                    var_kw = True
                if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
                    continue
                params.append(p.name)
                if p.default is inspect._empty:
                    required.append(p.name)
            sigs[name] = {"params": params, "required": required, "var_kw": var_kw}
        return sigs

    @staticmethod
    def coerce_args_to_signature(func_sig, args):
        params = func_sig.get("params", [])
        var_kw = func_sig.get("var_kw", False)
        if var_kw:
            return dict(args or {}), {"coerced": False, "reason": "var_kw_accepts_any"}
        if not params:
            return {}, {"coerced": False, "reason": "no_params"}
        args = dict(args or {})
        keys = list(args.keys())
        if len(params) == 1 and len(keys) == 1 and keys[0] not in params:
            coerced = {params[0]: args[keys[0]]}
            return coerced, {"coerced": True, "reason": "single_param_rename", "from": keys[0], "to": params[0]}
        filtered = {k: v for k, v in args.items() if k in params}
        removed = [k for k in keys if k not in params]
        info = {"coerced": bool(removed), "removed_keys": removed} if removed else {"coerced": False}
        return filtered, info

    def execute_tool(self, name, args=None):
        if hasattr(self.tool_impl, name):
            func = getattr(self.tool_impl, name)
            return func(**args) if args else func()
        return {"ok": False, "error": f"unknown tool {name}"}

    # ---------- Minimal-change helper to avoid Anthropic cache_control limit ----------
    @staticmethod
    def _is_anthropic_model(model) -> bool:
        name = getattr(model, "model_name", None) or getattr(model, "model", None) or ""
        name = (name or "").lower()
        return ("claude" in name) or ("sonnet" in name) or ("anthropic" in name)

    @staticmethod
    def _strip_cache_control_from_messages(messages):
        msgs = deepcopy(messages)
        for m in msgs:
            content = m.get("content")
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict):
                        blk.pop("cache_control", None)
        return msgs
    # -------------------------------------------------------------------------------

    def fc_agentic_loop(self, model, base_messages, agentic_tools, max_steps=8, allow_repeat=False):
        messages = list(base_messages)
        sigs = self.introspect_tool_signatures(self.tool_impl)
        seen = set()
        results = []
        did_submit = False
        final_text = None

        messages.insert(0, {
            "role": "system",
            "content": "Use at most one tool call per turn. Prefer tool calls over natural language. If a tool was already used, choose a different one."
        })

        previous_response_id = None
        model_name = getattr(model, "model_name", "")
        is_gpt5_responses = (model_name == "gpt-5")  # Responses API path

        for step in range(max_steps):
            # Minimal change: strip cache_control only for Anthropic models
            send_messages = self._strip_cache_control_from_messages(messages) if self._is_anthropic_model(model) else messages

            resp = model.generate_with_tools(
                messages=send_messages,
                tools=agentic_tools,
                previous_response_id=previous_response_id,
                max_tokens=self.max_tokens,
            )
            if isinstance(resp, dict):
                previous_response_id = resp.get("id")
                out = resp.get("output", []) or []
            else:
                previous_response_id = getattr(resp, "id", None)
                out = getattr(resp, "output", None) or []

            assistant_text = None
            for item in out:
                if self.get(item, "type") == "message":
                    content = self.get(item, "content", []) or []
                    for c in content:
                        if self.get(c, "type") == "output_text":
                            t = self.get(c, "text") or ""
                            if t:
                                assistant_text = (assistant_text or "") + t

            calls = [x for x in out if self.get(x, "type") == "function_call"]
            if not calls:
                final_text = (assistant_text or "").strip()
                break

            call = calls[0]
            name = self.get(call, "name")
            args_text = self.get(call, "arguments") or "{}"
            call_id = self.get(call, "call_id")

            try:
                args = json.loads(args_text) if isinstance(args_text, str) else (args_text or {})
            except Exception:
                args = {}

            # Echo of assistant tool call:
            if is_gpt5_responses:
                # Responses API does not accept assistant messages with tool_calls in input.
                messages.append({
                    "role": "assistant",
                    "content": json.dumps({"name": name, "args": args}, ensure_ascii=False)
                })
            else:
                messages.append({
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
                        }
                    ]
                })

            if not allow_repeat and name in seen:
                err = {"ok": False, "error": "tool '{}' already used once; pick a different tool".format(name)}
                results.append({
                    "index": step, "name": name, "args": args,
                    "arg_fixup": {"coerced": False}, "used_args": {}, "result": err
                })
                if is_gpt5_responses:
                    messages.append({"role": "tool", "content": json.dumps(err, ensure_ascii=False)})
                else:
                    messages.append({"role": "tool", "content": json.dumps(err, ensure_ascii=False), "tool_call_id": call_id})
                continue

            if not hasattr(self.tool_impl, name):
                err = {"ok": False, "error": "unknown tool {}".format(name)}
                results.append({
                    "index": step, "name": name, "args": args,
                    "arg_fixup": {"coerced": False}, "used_args": {}, "result": err
                })
                if is_gpt5_responses:
                    messages.append({"role": "tool", "content": json.dumps(err, ensure_ascii=False)})
                else:
                    messages.append({"role": "tool", "content": json.dumps(err, ensure_ascii=False), "tool_call_id": call_id})
                continue

            func_sig = sigs.get(name, {"params": [], "required": [], "var_kw": True})
            coerced_args, fix_info = self.coerce_args_to_signature(func_sig, args)

            try:
                res = self.execute_tool(name, coerced_args)
            except TypeError as e:
                res = {"ok": False, "error": "TypeError: {}".format(e), "trace": traceback.format_exc()}
            except Exception as e:
                res = {"ok": False, "error": "Exception: {}".format(e), "trace": traceback.format_exc()}

            results.append({
                "index": step, "name": name, "args": args,
                "arg_fixup": fix_info, "used_args": coerced_args, "result": res
            })
            seen.add(name)

            if is_gpt5_responses:
                messages.append({"role": "tool", "content": json.dumps(res, ensure_ascii=False)})
            else:
                messages.append({"role": "tool", "content": json.dumps(res, ensure_ascii=False), "tool_call_id": call_id})

            if name in self.terminal_names and isinstance(res, dict) and res.get("ok"):
                maybe_receipt = (
                    res.get("receipt")
                    or res.get("adoption_receipt")
                    or res.get("message")
                    or res.get("text")
                )
                if isinstance(maybe_receipt, str):
                    final_text = maybe_receipt
                elif isinstance(maybe_receipt, dict):
                    final_text = json.dumps(maybe_receipt, ensure_ascii=False)
                else:
                    final_text = json.dumps(res, ensure_ascii=False)
                did_submit = True
                break

        return {
            "messages": messages,
            "results": results,
            "did_submit": did_submit,
            "final_text": final_text
        }

    def json_agentic_loop(self, model, base_messages, tools_schema_json, max_steps=8, allow_repeat=False):
        messages = deepcopy(base_messages)
        sigs = self.introspect_tool_signatures(self.tool_impl)
        seen = set()
        results = []
        did_submit = False
        final_text = None

        messages.insert(0, {
            "role": "system",
            "content": "You are an agent. At each turn, output exactly one JSON object: {\"name\":\"<tool_name>\",\"args\":{...}}. Do not output extra text. Use each tool at most once. Available tools:\n" + tools_schema_json
        })

        previous_response_id = None

        for step in range(max_steps):
            # Minimal change: strip cache_control only for Anthropic models
            send_messages = self._strip_cache_control_from_messages(messages) if self._is_anthropic_model(model) else messages

            resp = model.generate_with_tools(send_messages, tools=None, previous_response_id=previous_response_id, max_tokens=self.max_tokens)
            if isinstance(resp, dict):
                previous_response_id = resp.get("id")
                out = resp.get("output", []) or []
            else:
                previous_response_id = getattr(resp, "id", None)
                out = getattr(resp, "output", None) or []

            raw = self.collect_output_text(out)
            s = self.coerce_json_block(raw)
            if not s:
                final_text = raw.strip()
                break

            try:
                call = json.loads(s)
            except Exception:
                final_text = raw.strip()
                break

            name = call.get("name")
            args = call.get("args") or {}

            if not isinstance(name, str):
                continue

            if not allow_repeat and name in seen:
                err = {"ok": False, "error": "tool '{}' already used once; pick a different tool".format(name)}
                results.append({
                    "index": step, "name": name, "args": args,
                    "arg_fixup": {"coerced": False}, "used_args": {}, "result": err
                })
                messages.append({"role": "assistant", "content": json.dumps(call, ensure_ascii=False)})
                messages.append({"role": "user", "content": json.dumps(err, ensure_ascii=False)})
                continue

            if not hasattr(self.tool_impl, name):
                err = {"ok": False, "error": "unknown tool {}".format(name)}
                results.append({
                    "index": step, "name": name, "args": args,
                    "arg_fixup": {"coerced": False}, "used_args": {}, "result": err
                })
                messages.append({"role": "assistant", "content": json.dumps(call, ensure_ascii=False)})
                messages.append({"role": "user", "content": json.dumps(err, ensure_ascii=False)})
                continue

            func_sig = self.introspect_tool_signatures(self.tool_impl).get(name, {"params": [], "required": [], "var_kw": True})
            coerced_args, fix_info = self.coerce_args_to_signature(func_sig, args)

            try:
                res = self.execute_tool(name, coerced_args)
            except TypeError as e:
                res = {"ok": False, "error": "TypeError: {}".format(e), "trace": traceback.format_exc()}
            except Exception as e:
                res = {"ok": False, "error": "Exception: {}".format(e), "trace": traceback.format_exc()}

            results.append({
                "index": step, "name": name, "args": args,
                "arg_fixup": fix_info, "used_args": coerced_args, "result": res
            })
            seen.add(name)

            messages.append({"role": "assistant", "content": json.dumps(call, ensure_ascii=False)})
            messages.append({"role": "user", "content": json.dumps(res, ensure_ascii=False)})

            if name in self.terminal_names and isinstance(res, dict) and res.get("ok"):
                did_submit = True
                maybe_receipt = res.get("receipt") or res.get("message") or res.get("text")
                if isinstance(maybe_receipt, str):
                    final_text = maybe_receipt
                elif isinstance(maybe_receipt, dict):
                    final_text = json.dumps(maybe_receipt, ensure_ascii=False)
                else:
                    final_text = json.dumps(res, ensure_ascii=False)
                break

        return {
            "messages": messages,
            "results": results,
            "did_submit": did_submit,
            "final_text": final_text
        }
