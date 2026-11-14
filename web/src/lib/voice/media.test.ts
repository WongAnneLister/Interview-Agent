import { describe, expect, it } from "vitest";
import { ApiError } from "../api/errors";
import { classifyVoiceError, concreteInputId, levelFromSamples } from "./media";

function domError(name: string) {
  const error = new Error("x");
  error.name = name;
  return error;
}

describe("classifyVoiceError", () => {
  it.each([
    ["NotAllowedError", "permission"],
    ["PermissionDeniedError", "permission"],
    ["SecurityError", "permission"],
    ["NotFoundError", "no_device"],
    ["DevicesNotFoundError", "no_device"],
    ["OverconstrainedError", "no_device"],
    ["NotReadableError", "device_busy"],
    ["TrackStartError", "device_busy"],
    ["AbortError", "device_busy"],
    ["TypeError", "unknown"],
  ])("把 %s 归为 %s", (name, kind) => {
    const result = classifyVoiceError(domError(name));
    expect(result.kind).toBe(kind);
    expect(result.message).not.toBe("");
    expect(result.apiError).toBeNull();
  });

  it("LiveKit 拒绝 token 与连不上分开提示", () => {
    const rejected = Object.assign(domError("ConnectionError"), { reason: 0 });
    const unreachable = Object.assign(domError("ConnectionError"), {
      reason: 1,
    });
    expect(classifyVoiceError(rejected).kind).toBe("token");
    expect(classifyVoiceError(unreachable).kind).toBe("network");
  });

  it("后端错误保留原消息和 trace_id", () => {
    const api = new ApiError("这场面试已经结束", 409, "conflict", "abc123");
    const result = classifyVoiceError(api);
    expect(result).toMatchObject({
      kind: "api",
      message: "这场面试已经结束",
      hint: null,
    });
    expect(result.apiError?.traceId).toBe("abc123");
  });

  it("不是 Error 的值也能分类", () => {
    expect(classifyVoiceError("boom").kind).toBe("unknown");
    expect(classifyVoiceError({ name: "NotAllowedError" }).kind).toBe(
      "permission",
    );
    expect(classifyVoiceError(null).kind).toBe("unknown");
  });
});

describe("levelFromSamples", () => {
  it("静音和空输入为 0", () => {
    expect(levelFromSamples([])).toBe(0);
    expect(levelFromSamples(new Float32Array(128))).toBe(0);
  });

  it("满幅为 1，-60 dB 及以下为 0", () => {
    expect(levelFromSamples([1, -1, 1, -1])).toBe(1);
    expect(levelFromSamples([0.001, -0.001])).toBeCloseTo(0);
    expect(levelFromSamples([0.0001, -0.0001])).toBe(0);
  });

  it("-30 dB 落在中间", () => {
    const amplitude = 10 ** (-30 / 20);
    expect(levelFromSamples([amplitude, -amplitude])).toBeCloseTo(0.5);
  });
});

describe("concreteInputId", () => {
  const mac = { deviceId: "mac", groupId: "g-mac", kind: "audioinput" };
  const iphone = { deviceId: "iphone", groupId: "g-iphone", kind: "audioinput" };
  const speaker = { deviceId: "out", groupId: "g-mac", kind: "audiooutput" };

  it("把 Chrome 的“默认”换成同组的具体设备", () => {
    const def = { deviceId: "default", groupId: "g-mac", kind: "audioinput" };
    expect(concreteInputId([def, iphone, mac, speaker])).toBe("mac");
  });

  it("默认是 iPhone 时就固定 iPhone，不再跟着系统换", () => {
    const def = { deviceId: "default", groupId: "g-iphone", kind: "audioinput" };
    expect(concreteInputId([def, mac, iphone])).toBe("iphone");
  });

  it("没有“默认”这一项时取第一个具体设备，跳过虚拟项", () => {
    const comms = { deviceId: "communications", groupId: "g-mac", kind: "audioinput" };
    expect(concreteInputId([comms, mac, iphone])).toBe("mac");
  });

  it("没有麦克风时返回 undefined", () => {
    expect(concreteInputId([speaker])).toBeUndefined();
    expect(concreteInputId([])).toBeUndefined();
  });
});
