"use client";
import { useEffect, useReducer, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import type { components } from "../api/schema";
import { textWebSocketUrl } from "../api/websocket";
import { createConnection } from "./connection";
import { controls, initialSessionState, reduce } from "./session";
type Command = components["schemas"]["CommandMessage"]["command"];
// 发出后等服务端的 state / error / ended 解锁；服务端若把命令当空操作不回任何消息，
// 到时间也解锁，避免按钮永久禁用。
const PENDING_TIMEOUT_MS = 10_000;
export function useInterviewSession(id: string) {
  const router = useRouter();
  const [state, dispatch] = useReducer(reduce, initialSessionState);
  const [attempt, setAttempt] = useState(0);
  const [generation, setGeneration] = useState(0);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const pendingTimer = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  const connection = useRef<ReturnType<typeof createConnection> | null>(null);
  function release() {
    clearTimeout(pendingTimer.current);
    pendingTimer.current = undefined;
    pendingRef.current = false;
    setPending(false);
  }
  useEffect(() => {
    // StrictMode 先清理再重跑 effect；延迟到下一任务，避免打断服务端开场白。
    const start = setTimeout(() => {
      connection.current = createConnection({
        url: textWebSocketUrl(
          process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000",
          id,
        ),
        onMessage(message) {
          if (
            message.type === "state" ||
            message.type === "error" ||
            message.type === "ended"
          ) {
            release();
          }
          dispatch({ type: "server_message", message });
        },
        onStatus(status, retryAttempt) {
          release();
          setAttempt(retryAttempt);
          dispatch({ type: "connection_changed", connection: status });
        },
      });
    }, 0);
    const tick = setInterval(() => dispatch({ type: "tick" }), 1000);
    return () => {
      release();
      clearTimeout(start);
      clearInterval(tick);
      connection.current?.close();
      connection.current = null;
    };
  }, [id, generation]);
  useEffect(() => {
    if (state.connection === "ended") {
      router.replace(`/interviews/${encodeURIComponent(id)}/report`);
    }
  }, [id, router, state.connection]);
  useEffect(() => {
    if (state.connection !== "open" && state.connection !== "reconnecting") return;
    function leave(event: BeforeUnloadEvent) {
      event.preventDefault();
      event.returnValue = "面试会保留，回来可以继续。";
    }
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [state.connection]);
  function send(message: components["schemas"]["TextClientMessage"]): boolean {
    if (pendingRef.current || !connection.current?.send(message)) return false;
    pendingRef.current = true;
    setPending(true);
    pendingTimer.current = setTimeout(release, PENDING_TIMEOUT_MS);
    return true;
  }
  const enabled = controls(state);
  return {
    state,
    attempt,
    pending,
    sendText(text: string) {
      if (
        !enabled.input || !text.trim() || Array.from(text).length > 4000 ||
        !send({ type: "user_text", text })
      ) return false;
      dispatch({ type: "sent_text", text });
      return true;
    },
    command(command: Command) {
      return enabled[command] && send({ type: "command", command });
    },
    reconnect() {
      dispatch({ type: "clear_error" });
      setGeneration((value) => value + 1);
    },
    clearError() {
      dispatch({ type: "clear_error" });
    },
  };
}
