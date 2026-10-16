"""Authenticated source-development access to the actual desktop assistant.

No model, credential, shell or arbitrary-object API lives in this module. Calls
are dispatched on the Qt thread and reuse the normal worker and conversation.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
from typing import Any

from coat_sim.agent.development import development_available, development_enabled
from coat_sim.ui.control import PROJECT_ROOT, UiControlClient, UiControlError, UiControlServer

DEBUG_OPERATIONS = frozenset({"status", "transcript", "events", "submit", "steer",
                              "withdraw", "cancel"})
MAX_PROMPT = 8000


class AssistantDebugClient(UiControlClient):
    """Discover a local endpoint without copying its authentication token."""

    def __init__(self, root=PROJECT_ROOT, target_pid=None, timeout=22.0):
        super().__init__(root, target_pid, timeout, operations=DEBUG_OPERATIONS,
                         endpoint_prefix="assistant-debug")


class AssistantDebugServer:
    def __init__(self, bridge, root=PROJECT_ROOT):
        self.bridge = bridge
        self.receipts: OrderedDict[str, dict] = OrderedDict()
        self.server = UiControlServer(self.dispatch, root=root, role="assistant-debug",
                                      operations=DEBUG_OPERATIONS,
                                      endpoint_prefix="assistant-debug")

    def start(self):
        if development_available():
            self.server.start()

    def close(self):
        self.server.close()

    @staticmethod
    def _integer(arguments, name, default, lower, upper):
        value = arguments.get(name, default)
        if type(value) is not int or not lower <= value <= upper:
            raise UiControlError("invalid_debug_argument", f"{name} 须为 {lower}–{upper} 的整数")
        return value

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> dict:
        enabled = development_enabled()
        if operation == "status":
            if not enabled:
                return {"available": development_available(), "enabled": False, "development_enabled": False,
                        "message": "在源码版设置中启用开发模式并保存后，才可调试。"}
            state = self.bridge.debug_status()
            state.pop("messages", None)
            state.pop("events", None)
            return {**state, "available": True, "development_enabled": True}
        if not enabled:
            raise UiControlError("development_disabled", "开发模式未启用或发行程序已禁用调试接口")
        if operation == "transcript":
            after = self._integer(arguments, "after_id", 0, 0, 2**63 - 1)
            limit = self._integer(arguments, "limit", 20, 1, 50)
            session = str(arguments.get("session_id") or self.bridge._session)
            messages = [row for row in self.bridge._store.messages(session) if row["id"] > after]
            page = messages[:limit]
            return {"session_id": session, "messages": page,
                    "next_id": page[-1]["id"] if page else after,
                    "has_more": len(messages) > limit}
        if operation == "events":
            after = self._integer(arguments, "after_id", 0, 0, 2**63 - 1)
            limit = self._integer(arguments, "limit", 100, 1, 200)
            session = str(arguments.get("session_id") or self.bridge._session)
            with self.bridge._store._db() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT id,request_id,event,payload,created FROM task_events "
                    "WHERE session=? AND id>? ORDER BY id LIMIT ?", (session, after, limit + 1))]
            for row in rows:
                row["payload"] = json.loads(row["payload"])
            page = rows[:limit]
            return {"session_id": session, "events": page,
                    "next_id": page[-1]["id"] if page else after,
                    "has_more": len(rows) > limit}
        if operation in ("submit", "steer"):
            prompt = arguments.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
                raise UiControlError("invalid_debug_prompt", "请输入 1–8000 字符的调试消息")
            message_id = arguments.get("client_message_id")
            if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128:
                raise UiControlError("id_required", "提交须提供唯一 client_message_id；超时后用相同 ID 查询/重试")
            key = operation + ":" + message_id
            fingerprint = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
            if key in self.receipts:
                prior = self.receipts[key]
                if prior["fingerprint"] != fingerprint:
                    raise UiControlError("id_conflict", "相同 client_message_id 的内容不能改变")
                return {**prior["result"], "replayed": True}
            if operation == "steer":
                result = self.bridge.debug_steer(prompt)
            else:
                limits = {"max_output_tokens": self._integer(arguments, "max_output_tokens", 4096, 256, 8192),
                          "request_limit": self._integer(arguments, "request_limit", 12, 1, 24),
                          "total_tokens_limit": self._integer(arguments, "total_tokens_limit", 60000, 1000, 200000)}
                for flag in ("new_session", "queue"):
                    if flag in arguments and type(arguments[flag]) is not bool:
                        raise UiControlError("invalid_debug_argument", f"{flag} 须为布尔值")
                result = self.bridge.debug_submit(prompt, {
                    "new_session": arguments.get("new_session", False),
                    "queue": arguments.get("queue", False),
                    "mode": arguments.get("mode", "operate"),
                    "reasoning": arguments.get("reasoning", "low"),
                    "limits": limits})
            # A refused request can still have created a session or recorded an
            # attempt. Preserve that receipt too; corrected attempts need a new ID.
            self.receipts[key] = {"fingerprint": fingerprint, "result": result}
            while len(self.receipts) > 128:
                self.receipts.popitem(last=False)
            return result
        if operation == "withdraw":
            message_id = str(arguments.get("message_id", ""))
            if not message_id:
                raise UiControlError("id_required", "撤回须提供 message_id")
            return self.bridge.debug_withdraw(message_id)
        if operation == "cancel":
            request_id = arguments.get("request_id")
            if request_id != self.bridge._request_id or not self.bridge.busy:
                raise UiControlError("request_mismatch", "仅能中止当前指定 request_id 的任务")
            self.bridge.cancelTask()
            return {"cancel_requested": True, "request_id": request_id}
        raise UiControlError("unsupported_debug_operation", "未知调试操作")


def main():
    parser = argparse.ArgumentParser(description="源码版看板娘本地调试；须先启用开发模式")
    parser.add_argument("operation", choices=sorted(DEBUG_OPERATIONS))
    parser.add_argument("--pid", type=int)
    parser.add_argument("--arguments", default="{}", help="JSON 参数；不包含任何密钥")
    args = parser.parse_args()
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        parser.error("arguments 须为 JSON 对象")
    result = AssistantDebugClient(target_pid=args.pid).request(args.operation, **arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
