"use client";

import {
  ConnectionState,
  Room,
  RoomEvent,
  Track,
  createLocalAudioTrack,
  type LocalAudioTrack,
  type RemoteTrack,
} from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { createVoiceSession } from "../api/client";
import { classifyVoiceError, concreteInputId, failure, type VoiceFailure } from "./media";

export type VoicePhase =
  | "idle" // 还没开始
  | "checking" // 麦克风自检中（未入房）
  | "joining" // 申请麦克风、取 token、连接房间
  | "connected"
  | "reconnecting"
  | "left" // 主动离开或被服务端断开
  | "failed";

// 浏览器自带的回声消除、降噪、自动增益（外放时尤其需要回声消除）
const CAPTURE = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
} as const;

// 麦克风用具体设备而不是“default”：livekit-client 在系统默认输入变化时会把 default 上的音轨
// 切到新设备（macOS 连续互通会在面试中途把 iPhone 设成默认，PR-30 实测）。发布时它记下的是
// 音轨实际用的设备，所以在发布之前就用具体设备建音轨，之后不会跟着切，也不用重启已发布的音轨
// （上一版发布后再 switchActiveDevice，重启音轨后识别几乎收不到声音，PR-30 实测）。
async function openMicrophone(): Promise<LocalAudioTrack> {
  const track = await createLocalAudioTrack(CAPTURE); // 先拿到权限，设备列表才有名称和分组
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const deviceId = concreteInputId(devices);
    if (!deviceId || track.mediaStreamTrack.getSettings().deviceId === deviceId) return track;
    const pinned = await createLocalAudioTrack({ ...CAPTURE, deviceId: { exact: deviceId } });
    track.stop();
    return pinned;
  } catch {
    return track; // 固定失败时继续用默认设备
  }
}

function mediaAvailable(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

export function useVoiceRoom(id: string) {
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [error, setError] = useState<VoiceFailure | null>(null);
  const [mic, setMic] = useState<LocalAudioTrack | null>(null);
  const [canPlayAudio, setCanPlayAudio] = useState(true);
  const [peers, setPeers] = useState(0);
  const room = useRef<Room | null>(null);
  const micRef = useRef<LocalAudioTrack | null>(null);
  const generation = useRef(0); // 卸载或离开后，进行中的 join 不再改状态

  const releaseMic = useCallback(() => {
    micRef.current?.stop();
    micRef.current = null;
    setMic(null);
  }, []);

  const teardown = useCallback(async () => {
    generation.current += 1;
    const current = room.current;
    room.current = null;
    micRef.current?.stop();
    micRef.current = null;
    if (current) await current.disconnect();
  }, []);

  useEffect(() => () => void teardown(), [teardown]);

  const acquireMic = useCallback(async (): Promise<LocalAudioTrack> => {
    if (micRef.current) return micRef.current;
    if (!mediaAvailable()) throw failure("insecure");
    const track = await openMicrophone();
    micRef.current = track;
    setMic(track);
    return track;
  }, []);

  const fail = useCallback(
    (cause: unknown) => {
      const result =
        typeof cause === "object" && cause !== null && "kind" in cause
          ? (cause as VoiceFailure)
          : classifyVoiceError(cause);
      setError(result);
      setPhase("failed");
    },
    [],
  );

  // 入房前的麦克风自检：只申请麦克风，不取 token、不建房间（建房会派发面试官）。
  const startCheck = useCallback(async () => {
    setError(null);
    setPhase("checking");
    try {
      await acquireMic();
    } catch (cause) {
      releaseMic();
      fail(cause);
    }
  }, [acquireMic, fail, releaseMic]);

  const stopCheck = useCallback(() => {
    releaseMic();
    setPhase("idle");
  }, [releaseMic]);

  // 必须在点击事件里调用：先拿麦克风，确认可用后再取 token 入房。
  const join = useCallback(async () => {
    const token = ++generation.current;
    const stale = () => token !== generation.current;
    setError(null);
    setPhase("joining");
    const next = new Room({ audioCaptureDefaults: CAPTURE });
    try {
      const track = await acquireMic();
      const session = await createVoiceSession(id);
      if (stale()) return;
      next
        .on(RoomEvent.ConnectionStateChanged, (state) => {
          if (stale()) return;
          if (state === ConnectionState.Connected) setPhase("connected");
          else if (
            state === ConnectionState.Reconnecting ||
            state === ConnectionState.SignalReconnecting
          )
            setPhase("reconnecting");
        })
        .on(RoomEvent.Disconnected, () => {
          if (stale()) return;
          room.current = null;
          setPhase("left");
        })
        .on(RoomEvent.AudioPlaybackStatusChanged, () =>
          setCanPlayAudio(next.canPlaybackAudio),
        )
        .on(RoomEvent.ParticipantConnected, () => setPeers(next.remoteParticipants.size))
        .on(RoomEvent.ParticipantDisconnected, () => setPeers(next.remoteParticipants.size))
        .on(RoomEvent.TrackSubscribed, (remote: RemoteTrack) => {
          if (remote.kind !== Track.Kind.Audio) return;
          const element = remote.attach();
          element.hidden = true;
          document.body.appendChild(element);
        })
        .on(RoomEvent.TrackUnsubscribed, (remote: RemoteTrack) => {
          remote.detach().forEach((element) => element.remove());
        });
      room.current = next;
      await next.connect(session.url, session.token);
      if (stale()) return;
      await next.localParticipant.publishTrack(track, {
        source: Track.Source.Microphone,
      });
      if (stale()) return;
      setCanPlayAudio(next.canPlaybackAudio);
      setPeers(next.remoteParticipants.size);
      setPhase("connected");
    } catch (cause) {
      if (stale()) return;
      room.current = null;
      await next.disconnect();
      releaseMic();
      fail(cause);
    }
  }, [acquireMic, fail, id, releaseMic]);

  const leave = useCallback(async () => {
    await teardown();
    setMic(null);
    setPeers(0);
    setPhase("left");
  }, [teardown]);

  // 浏览器拦截了自动播放时，由用户再点一次开启声音
  const enableAudio = useCallback(async () => {
    await room.current?.startAudio();
    setCanPlayAudio(room.current?.canPlaybackAudio ?? true);
  }, []);

  return {
    phase,
    error,
    micTrack: mic?.mediaStreamTrack ?? null,
    canPlayAudio,
    peers,
    startCheck,
    stopCheck,
    join,
    leave,
    enableAudio,
  };
}
