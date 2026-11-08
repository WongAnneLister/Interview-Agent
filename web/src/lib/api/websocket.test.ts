import { expect, it } from "vitest";
import { textWebSocketUrl } from "./websocket";
it.each([
  ["http://localhost:8000", "ws://localhost:8000"],
  ["https://example.test/", "wss://example.test"],
])("转换 %s", (base, expected) => {
  expect(textWebSocketUrl(base, "a b")).toBe(`${expected}/ws/interviews/a%20b/text`);
});
