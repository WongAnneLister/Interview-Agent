"use client";

import { useEffect, useState } from "react";
import { getReport, getTranscript, regenerateReport } from "../api/client";
import { ApiError, toTransportError } from "../api/errors";
import type { components } from "../api/schema";
import { nextPoll, type PollState } from "../report";

type S = components["schemas"];

export function useReport(id: string) {
  const [request, setRequest] = useState({ revision: 0, regenerate: false });
  const [status, setStatus] = useState<
    "loading" | "pending" | "failed" | "ready" | "error" | "not_ended"
  >("loading");
  const [data, setData] = useState<S["ReportOut"] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [transcript, setTranscript] = useState<S["TranscriptOut"] | null>(null);
  const [transcriptError, setTranscriptError] = useState<ApiError | null>(null);
  const [transcriptRevision, setTranscriptRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let pollState: PollState = { attempt: 0, errors: 0 };
    async function poll() {
      let result: S["ReportOut"] | ApiError;
      try {
        result = await getReport(id, controller.signal);
      } catch (cause) {
        result = toTransportError(cause, controller.signal);
      }
      if (!active) return;
      const decision = nextPoll(pollState, result);
      if (!(result instanceof ApiError)) setData(result);
      if (decision.kind === "wait") {
        pollState = decision.state;
        setStatus("pending");
        timer = setTimeout(() => void poll(), decision.delayMs);
      } else if (decision.kind === "done") {
        if (result instanceof ApiError || !result.report) {
          setError(new ApiError("报告内容为空，请重试", null, "invalid_response"));
          setStatus("error");
        } else {
          setStatus("ready");
        }
      } else {
        setStatus(decision.kind);
        if (decision.kind === "error") setError(decision.error);
      }
    }
    async function start() {
      setError(null);
      if (request.regenerate) {
        setRegenerating(true);
        try {
          await regenerateReport(id, controller.signal);
        } catch (cause) {
          const failure = toTransportError(cause, controller.signal);
          if (!active) return;
          if (failure.status !== 409) {
            setError(failure);
            setRegenerating(false);
            return;
          }
        }
        if (!active) return;
        setRegenerating(false);
        setStatus("pending");
      } else {
        setStatus("loading");
      }
      await poll();
    }
    void start();
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [id, request]);

  useEffect(() => {
    if (status !== "ready") return;
    const controller = new AbortController();
    let active = true;
    async function load() {
      setTranscriptError(null);
      try {
        const value = await getTranscript(id, controller.signal);
        if (active) setTranscript(value);
      } catch (cause) {
        if (active) setTranscriptError(toTransportError(cause, controller.signal));
      }
    }
    void load();
    return () => {
      active = false;
      controller.abort();
    };
  }, [id, status, transcriptRevision]);

  return {
    status, data, error, regenerating, transcript, transcriptError,
    retry: () => setRequest(value => ({ revision: value.revision + 1, regenerate: false })),
    regenerate: () => setRequest(value => ({ revision: value.revision + 1, regenerate: true })),
    retryTranscript: () => setTranscriptRevision(value => value + 1),
  };
}
