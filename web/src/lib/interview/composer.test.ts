import { expect, it } from "vitest";
import { shouldSubmit } from "./composer";
it.each([
  ["Enter", false, false, 13, true],
  ["Enter", true, false, 13, false],
  ["Enter", false, true, 13, false],
  ["Enter", false, false, 229, false],
  ["a", false, false, 65, false],
])(
  "键盘 %s shift=%s composing=%s code=%s",
  (key, shiftKey, isComposing, keyCode, expected) => {
    expect(shouldSubmit({ key, shiftKey, isComposing, keyCode })).toBe(expected);
  },
);
