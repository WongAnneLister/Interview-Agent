// 语音入房的纯逻辑：失败分类和麦克风音量。不依赖浏览器 API，便于单元测试。
import { ApiError } from "../api/errors";

export type VoiceFailureKind =
  | "insecure" // 非安全上下文，浏览器不提供麦克风接口
  | "permission" // 用户或系统拒绝了麦克风权限
  | "no_device" // 没有可用的麦克风
  | "device_busy" // 麦克风被其它程序占用
  | "network" // 连不上 LiveKit
  | "token" // 入房凭证被 LiveKit 拒绝
  | "api" // 后端接口返回错误（带 trace_id）
  | "unknown";

export interface VoiceFailure {
  kind: VoiceFailureKind;
  message: string;
  hint: string | null;
  apiError: ApiError | null;
}

const messages: Record<
  Exclude<VoiceFailureKind, "api">,
  { message: string; hint: string | null }
> = {
  insecure: {
    message: "当前页面无法使用麦克风",
    hint: "浏览器只在 localhost 或 HTTPS 页面开放麦克风，请用 http://localhost:3000 打开。",
  },
  permission: {
    message: "麦克风权限被拒绝",
    hint: "点击地址栏左侧的权限图标，允许使用麦克风后重试；macOS 还需要在“系统设置 → 隐私与安全性 → 麦克风”里允许浏览器。",
  },
  no_device: {
    message: "没有找到可用的麦克风",
    hint: "请连接耳机麦克风或外置麦克风后重试。",
  },
  device_busy: {
    message: "麦克风正被其它程序占用",
    hint: "关闭正在使用麦克风的会议软件或录音程序后重试。",
  },
  network: {
    message: "连不上语音服务",
    hint: "请确认已运行 make livekit，且网络或代理没有拦截 WebRTC。",
  },
  token: {
    message: "语音房间拒绝了入房凭证",
    hint: "凭证可能已过期，或 LiveKit 的密钥与后端 .env 不一致，请重试。",
  },
  unknown: {
    message: "语音连接失败",
    hint: "请刷新页面后重试。",
  },
};

export function failure(kind: Exclude<VoiceFailureKind, "api">): VoiceFailure {
  return { kind, ...messages[kind], apiError: null };
}

function name(error: unknown): string {
  return error instanceof Error ||
    (typeof error === "object" && error !== null && "name" in error)
    ? String((error as { name: unknown }).name)
    : "";
}

// livekit-client 的 ConnectionErrorReason.NotAllowed（token 无效、过期或无权限）
const CONNECTION_NOT_ALLOWED = 0;

export function classifyVoiceError(error: unknown): VoiceFailure {
  if (error instanceof ApiError) {
    return { kind: "api", message: error.message, hint: null, apiError: error };
  }
  switch (name(error)) {
    // getUserMedia 的 DOMException（含旧版 Chrome 的别名）
    case "NotAllowedError":
    case "PermissionDeniedError":
    case "SecurityError":
      return failure("permission");
    case "NotFoundError":
    case "DevicesNotFoundError":
    case "OverconstrainedError":
      return failure("no_device");
    case "NotReadableError":
    case "TrackStartError":
    case "AbortError":
      return failure("device_busy");
    case "ConnectionError":
      return (error as { reason?: unknown }).reason === CONNECTION_NOT_ALLOWED
        ? failure("token")
        : failure("network");
    default:
      return failure("unknown");
  }
}

// 时域采样（-1..1）→ 0..1 的音量。按分贝线性映射，-60 dB 以下视为静音，
// 这样正常说话时音量条能到一半以上，而不是只在最左边抖动。
export const SILENCE_DB = -60;

export function levelFromSamples(samples: ArrayLike<number>): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    const sample = samples[i] ?? 0;
    sum += sample * sample;
  }
  const rms = Math.sqrt(sum / samples.length);
  if (rms === 0) return 0;
  const db = 20 * Math.log10(rms);
  return Math.min(1, Math.max(0, (db - SILENCE_DB) / -SILENCE_DB));
}
