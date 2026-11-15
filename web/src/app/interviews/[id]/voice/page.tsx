"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { buttonDanger, buttonPrimary, buttonSecondary } from "@/lib/styles";
import { useMicLevel } from "@/lib/voice/useMicLevel";
import { useVoiceRoom, type VoicePhase } from "@/lib/voice/useVoiceRoom";

const phaseLabels: Record<VoicePhase, string> = {
  idle: "未开始",
  checking: "正在测试麦克风",
  joining: "正在进入…",
  connected: "通话中",
  reconnecting: "网络不稳，正在重连…",
  left: "已离开",
  failed: "连接失败",
};

export default function VoiceInterviewPage() {
  const { id } = useParams<{ id: string }>();
  return <VoiceRoom key={id} id={id} />;
}

function LevelMeter({ level }: { level: number }) {
  const percent = Math.min(100, Math.round(level * 100));
  return (
    <div
      role="meter"
      aria-label="麦克风音量"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      className="h-1.5 w-full overflow-hidden rounded-sm bg-line"
    >
      <div
        className="h-full bg-accent transition-[width] duration-75"
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
  const live = room.phase === "connected";

  return (
    <section className="flex min-w-0 flex-col gap-10">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-3">
          <h1 className="text-[32px] leading-tight text-ink sm:text-[38px]">语音面试</h1>
          <p className="text-muted">像真实面试一样开口回答，说完停一下，面试官会接着问。</p>
        </div>
        <p role="status" className="flex items-center gap-2 text-sm text-muted">
          <span
            className={`size-2 rounded-full ${
              live ? "bg-accent" : room.phase === "reconnecting" || busy ? "bg-warn" : "bg-faint"
            }`}
          />
          {phaseLabels[room.phase]}
          {inRoom && (
            <span className="text-faint">
              · {room.peers > 0 ? "面试官在线" : "等待面试官"}
            </span>
          )}
        </p>
      </header>

      {inRoom && !room.canPlayAudio && (
        <div className="flex flex-wrap items-center justify-between gap-3 border-l-2 border-warn pl-3">
          <p className="text-sm text-warn-ink">浏览器拦下了自动播放，现在听不到面试官的声音。</p>
          <button className={buttonSecondary} onClick={() => void room.enableAudio()}>
            打开声音
          </button>
        </div>
      )}

      {room.error &&
        (room.error.apiError ? (
          <ErrorNotice error={room.error.apiError} onRetry={() => void room.join()} />
        ) : (
          <div role="alert" className="flex flex-col gap-2 border-l-2 border-danger pl-3">
            <p className="text-danger-ink">{room.error.message}</p>
            {room.error.hint && <p className="text-sm text-danger">{room.error.hint}</p>}
            <div>
              <button className={buttonSecondary} onClick={() => void room.join()}>
                重新连接
              </button>
            </div>
          </div>
        ))}

      {inRoom ? (
        <div className="flex flex-col items-center gap-8 border-y border-line py-14 text-center">
          <div className="relative flex size-32 items-center justify-center" aria-hidden="true">
            <span className="absolute inset-0 rounded-full border border-accent/40 motion-safe:animate-pulse-ring" />
            <span className="size-20 rounded-full bg-accent" />
          </div>
          <div>
            <p className="font-serif text-2xl text-ink">
              {room.peers > 0 ? "面试进行中" : "面试官马上就到"}
            </p>
            <p className="mt-2 text-sm text-muted">
              没听清可以直接说“请再说一遍”；想一想时说“我想一下”，说完了可以说“我说完了”。
            </p>
          </div>
          <div className="flex w-full max-w-xs flex-col gap-2">
            <LevelMeter level={level} />
            <span className="text-xs text-faint">你的麦克风</span>
          </div>
          <button className={buttonDanger} onClick={() => void room.leave()}>
            离开面试
          </button>
        </div>
      ) : (
        <div className="flex flex-col gap-10">
          <ul className="grid grid-cols-1 gap-x-10 border-t border-line sm:grid-cols-2">
            <li className="border-b border-line py-4">
              <p className="text-ink">戴上耳机</p>
              <p className="mt-1 text-sm text-muted">
                外放的声音会被麦克风收进去，可能被当成你的回答，或者打断面试官。
              </p>
            </li>
            <li className="border-b border-line py-4">
              <p className="text-ink">找个安静的地方</p>
              <p className="mt-1 text-sm text-muted">
                背景噪音会影响识别。中途离开后暂时不能接着这一场继续。
              </p>
            </li>
          </ul>

          <div className="flex flex-col gap-4">
            <h2 className="text-2xl text-ink">先试试麦克风</h2>
            {room.micTrack ? (
              <div className="flex max-w-md flex-col gap-2">
                <LevelMeter level={level} />
                <p className="text-sm text-muted">
                  说几句话，绿条跟着动就说明没问题；一直不动的话，检查一下浏览器和系统的麦克风权限。
                </p>
              </div>
            ) : room.phase === "checking" || busy ? (
              <p className="text-sm text-muted" role="status">
                正在打开麦克风。浏览器弹出权限请求时，选“允许”。
              </p>
            ) : (
              <p className="text-sm text-muted">只打开麦克风看音量，不会开始面试。</p>
            )}

            <div className="flex flex-wrap items-center gap-3 pt-2">
              {room.phase === "checking" ? (
                <button type="button" className={buttonSecondary} onClick={room.stopCheck}>
                  结束测试
                </button>
              ) : (
                <button
                  type="button"
                  className={buttonSecondary}
                  disabled={busy}
                  onClick={() => void room.startCheck()}
                >
                  测试麦克风
                </button>
              )}
              <button
                type="button"
                className={buttonPrimary}
                disabled={busy}
                onClick={() => void room.join()}
              >
                {busy ? "正在进入…" : room.phase === "left" ? "重新进入" : "开始语音面试"}
              </button>
            </div>
          </div>
        </div>
      )}

      <p className="text-sm text-muted">
        现在不方便说话？
        <Link
          className="ml-1 text-accent underline-offset-4 hover:underline"
          href={`/interviews/${encodeURIComponent(id)}/text`}
        >
          改用文字面试
        </Link>
      </p>
    </section>
  );
}
