"use client";

import { useEffect, useState } from "react";
import { levelFromSamples } from "./media";

// 读取麦克风音轨的实时音量（0..1），用于入房前自检和入房后的音量条。
export function useMicLevel(track: MediaStreamTrack | null): number {
  const [level, setLevel] = useState(0);
  useEffect(() => {
    if (!track) return;
    const context = new AudioContext();
    const source = context.createMediaStreamSource(new MediaStream([track]));
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    let frame = 0;
    const tick = () => {
      analyser.getFloatTimeDomainData(samples);
      setLevel(levelFromSamples(samples));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      source.disconnect();
      void context.close();
    };
  }, [track]);
  return track ? level : 0;
}
