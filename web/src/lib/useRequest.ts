"use client";
import { useEffect, useState } from "react";
import type { ApiError } from "./api/errors";
import { createRequestRunner } from "./request";

export function useRequest() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [runner] = useState(() => createRequestRunner(setBusy, setError));
  useEffect(() => runner.cancel, [runner]);
  return { ...runner, busy, error };
}
