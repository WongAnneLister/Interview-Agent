"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { buttonDanger, buttonPrimary, buttonSecondary, panel } from "@/lib/styles";
import { useMicLevel } from "@/lib/voice/useMicLevel";
import { useVoiceRoom, type VoicePhase } from "@/lib/voice/useVoiceRoom";

const phaseLabels: Record<VoicePhase, string> = {
  idle: "未连接",
  checking: "麦克风自检中",
  joining: "正在连接…",
  connected: "已连接",
  reconnecting: "网络波动，正在重连…",
  left: "已离开房间",
  failed: "未连接",
};

export default function VoiceInterviewPage() {
  const { id } = useParams<{ id: string }>();
  return <VoiceRoom key={id} id={id} />;
}

function LevelMeter({ level }: { level: number }) {
  const percent = Math.round(level * 100);
  return (
    <div
      role="meter"
      aria-label="麦克风音量"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      className="h-3 w-full overflow-hidden rounded-full bg-[#e3ebe6]"
    >
      <div
        className="h-full rounded-full bg-accent transition-[width] duration-75 motion-reduce:transition-none"
        style={{ width: `${percent}%` }}
      />
    </div>
  );
}

function VoiceRoom({ id }: { id: string }) {
  const room = useVoiceRoom(id);
  const level = useMicLevel(room.micTrack);
  const inRoom = room.phase === "connected" || room.phase === "reconnecting";
  const busy = room.phase === "joining";
  return (
    <section className="flex min-w-0 flex-col gap-5">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-bold">语音面试</h1>
        <p role="status" className="text-sm">
          {phaseLabels[room.phase]}
          {inRoom && (room.peers > 0 ? " · 面试官已进入房间" : " · 等待面试官进入房间")}
        </p>
      </header>

      {!inRoom && (
        <div className={`${panel} flex flex-col gap-3`}>
          <h2 className="font-bold">开始前</h2>
          <ul className="list-disc space-y-1 pl-5 text-sm">
            <li>
              <strong>佩戴耳机效果更好。</strong>
              外放时面试官的声音会被麦克风收进去，可能被误当成你的回答或打断。
            </li>
            <li>找一个安静的地方，面试中途可以随时离开房间。</li>
          </ul>
        </div>
      )}

      <div className={`${panel} flex flex-col gap-4`}>
        <h2 className="font-bold">{inRoom ? "你的麦克风" : "麦克风自检"}</h2>
        {room.micTrack ? (
          <>
            <LevelMeter level={level} />
            <p className="text-sm text-muted">
              对着麦克风说几句话，音量条应该跟着跳动；一直不动请检查系统选择的输入设备。
            </p>
          </>
        ) : room.phase === "checking" || busy ? (
          <p role="status" className="text-sm">
            正在请求麦克风……如果浏览器弹出了权限提示，请选择“允许”。
          </p>
        ) : (
          <p className="text-sm text-muted">
            点“测试麦克风”只检查设备，不会开始面试。
          </p>
        )}
        <div className="flex flex-wrap gap-3">
          {!inRoom &&
            (room.phase === "checking" ? (
              <button className={buttonSecondary} onClick={room.stopCheck}>
                结束自检
              </button>
            ) : (
              <button
                className={buttonSecondary}
                disabled={busy}
                onClick={() => void room.startCheck()}
              >
                测试麦克风
              </button>
            ))}
          {!inRoom ? (
            <button
              className={buttonPrimary}
              disabled={busy}
              onClick={() => void room.join()}
            >
              {busy ? "正在连接…" : room.phase === "left" ? "重新加入" : "开始语音面试"}
            </button>
          ) : (
            <button className={buttonDanger} onClick={() => void room.leave()}>
              离开房间
            </button>
          )}
        </div>
      </div>

      {inRoom && !room.canPlayAudio && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl bg-[#fff6e0] p-4">
          <p>浏览器拦截了自动播放，听不到面试官的声音。</p>
          <button className={buttonSecondary} onClick={() => void room.enableAudio()}>
            开启声音
          </button>
        </div>
      )}

      {room.error &&
        (room.error.apiError ? (
          <ErrorNotice error={room.error.apiError} onRetry={() => void room.join()} />
        ) : (
          <div
            role="alert"
            className="flex flex-col gap-3 rounded-xl border border-[#e6bbb0] bg-[#fff2ed] p-5"
          >
            <p className="font-semibold">{room.error.message}</p>
            {room.error.hint && <p className="text-sm">{room.error.hint}</p>}
            <div>
              <button className={buttonSecondary} onClick={() => void room.join()}>
                重试
              </button>
            </div>
          </div>
        ))}

      <p className="text-sm text-muted">
        不方便说话？
        <Link className="underline" href={`/interviews/${encodeURIComponent(id)}/text`}>
          改用文字面试
        </Link>
      </p>
    </section>
  );
}
