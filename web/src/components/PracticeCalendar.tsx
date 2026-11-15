"use client";

import Link from "next/link";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { listPracticeDays } from "@/lib/api/client";
import { interviewStatusLabels } from "@/lib/labels";
import {
  buildCalendar,
  localToday,
  practiceStats,
  type CalendarCell,
  type PracticeDay,
} from "@/lib/practice";

const levelClass = [
  "bg-line/70",
  "bg-accent-line",
  "bg-accent/55",
  "bg-accent/80",
  "bg-accent-ink",
] as const;
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
const CELL = 14; // 格子 11px + 间距 3px

function dateLabel(date: string): string {
  const [, month, day] = date.split("-").map(Number);
  const weekday = weekdays[(new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7];
  return `${month}月${day}日 周${weekday}`;
}

/** 数字从 0 滚到目标值；系统要求减少动画时直接显示。 */
function useCountUp(target: number, duration = 900): number {
  const [value, setValue] = useState(0);
  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const length = reduced ? 0 : duration;
    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = length ? Math.min(1, (now - start) / length) : 1;
      setValue(Math.round(target * (1 - (1 - t) ** 3)));
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return value;
}

function Stat({ value, label, unit }: { value: number; label: string; unit: string }) {
  const shown = useCountUp(value);
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-sm text-muted">{label}</span>
      <span className="font-serif text-2xl leading-none text-ink tabular-nums">{shown}</span>
      <span className="text-sm text-muted">{unit}</span>
    </div>
  );
}

function DayDetail({ day }: { day: PracticeDay }) {
  return (
    <div className="flex flex-col gap-2 border-l-2 border-accent pl-4">
      <p className="text-sm text-muted">
        {dateLabel(day.date)} · 答了 {day.answers} 轮
      </p>
      <ul className="flex flex-col gap-1">
        {day.sessions.map((session) => {
          const done = session.status === "completed" || session.status === "aborted";
          const href = `/interviews/${encodeURIComponent(session.interview_id)}/${done ? "report" : "plan"}`;
          return (
            <li key={session.interview_id} className="flex flex-wrap items-baseline gap-x-3">
              <Link className="text-ink underline-offset-4 hover:text-accent-ink hover:underline" href={href}>
                {session.job_title || "未注明岗位"}
              </Link>
              <span className="text-sm text-muted">
                {session.answers} 轮 · {interviewStatusLabels[session.status]}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function PracticeCalendar() {
  const [days, setDays] = useState<PracticeDay[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [hover, setHover] = useState<{ cell: CalendarCell; x: number; y: number } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const board = useRef<HTMLDivElement>(null);
  const today = useMemo(() => localToday(), []);

  useEffect(() => {
    const controller = new AbortController();
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    listPracticeDays(zone, controller.signal).then(setDays, () => {
      if (!controller.signal.aborted) setFailed(true);
    });
    return () => controller.abort();
  }, []);

  const calendar = useMemo(() => buildCalendar(days ?? [], today), [days, today]);
  const stats = useMemo(() => practiceStats(days ?? [], today), [days, today]);
  const chosen = days?.find((day) => day.date === selected) ?? null;

  // 窄屏时日历比屏幕宽：默认滚到最右边，先看到最近的日子
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [days]);

  if (failed) return null; // 只是锦上添花，读不到就不显示，不打扰准备面试

  function show(cell: CalendarCell, target: HTMLElement) {
    // 提示框约 150px 宽、以格子为中心：靠两边时往里收，不撑出横向滚动条
    const width = board.current?.offsetWidth ?? 0;
    const x = Math.min(Math.max(target.offsetLeft + 5.5, 76), width - 76);
    setHover({ cell, x, y: target.offsetTop });
  }

  return (
    <section className="flex min-w-0 flex-col gap-4" aria-labelledby="practice-title">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <h2 className="text-2xl text-ink" id="practice-title">
          练习记录
        </h2>
        {days && (
          <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
            <Stat value={stats.currentStreak} label="连续" unit="天" />
            <Stat value={stats.longestStreak} label="最长" unit="天" />
            <Stat value={stats.monthSessions} label="本月" unit="场" />
          </div>
        )}
      </div>

      <div ref={scroller} className="-mx-1 overflow-x-auto px-1 pb-1">
        <div
          ref={board}
          className="relative w-max"
          onMouseLeave={() => setHover(null)}
          style={{ minHeight: 7 * CELL + 22 }}
        >
          <div className="relative mb-1.5 ml-6 h-4 text-[11px] text-faint" aria-hidden="true">
            {calendar.months.map((month) => (
              <span key={month.column} className="absolute whitespace-nowrap" style={{ left: month.column * CELL }}>
                {month.label}
              </span>
            ))}
          </div>
          <div className="flex gap-[3px]">
            <div
              className="mr-1 grid w-5 grid-rows-7 gap-[3px] text-[10px] leading-[11px] text-faint"
              aria-hidden="true"
            >
              {weekdays.map((name, index) => (
                <span key={name}>{index % 2 === 0 ? name : ""}</span>
              ))}
            </div>
            {days &&
              calendar.weeks.map((week, column) => (
                <div key={week[0]?.date} className="grid grid-rows-7 gap-[3px]">
                  {week.map((cell, row) => {
                    if (cell.future) return <span key={cell.date} className="size-[11px]" />;
                    const isToday = cell.date === today;
                    const motion = isToday ? "cell-today" : cell.level > 0 ? "cell-pop" : "cell-in";
                    const style = { animationDelay: `${Math.min(900, (column + row) * 14)}ms` };
                    const common = `size-[11px] rounded-[2px] ${levelClass[cell.level]} ${motion}`;
                    if (cell.level === 0) {
                      return (
                        <span
                          key={cell.date}
                          className={common}
                          style={style}
                          onMouseEnter={(event) => show(cell, event.currentTarget)}
                        />
                      );
                    }
                    return (
                      <button
                        key={cell.date}
                        type="button"
                        aria-label={`${dateLabel(cell.date)}，答了 ${cell.answers} 轮`}
                        aria-pressed={selected === cell.date}
                        className={`${common} cursor-pointer transition-transform duration-150 hover:scale-[1.35] focus-visible:scale-[1.35] aria-pressed:outline-2 aria-pressed:outline-offset-1 aria-pressed:outline-ink`}
                        style={style}
                        onMouseEnter={(event) => show(cell, event.currentTarget)}
                        onFocus={(event) => show(cell, event.currentTarget)}
                        onBlur={() => setHover(null)}
                        onClick={() => setSelected((value) => (value === cell.date ? null : cell.date))}
                      />
                    );
                  })}
                </div>
              ))}
          </div>

          {hover && (
            <div
              key={hover.cell.date}
              role="tooltip"
              className="tip-in pointer-events-none absolute z-10 rounded-md bg-ink px-2.5 py-1.5 text-xs whitespace-nowrap text-paper"
              style={{ left: hover.x, top: hover.y - 34 }}
            >
              {dateLabel(hover.cell.date)}
              <span className="ml-1.5 text-paper/70">
                {hover.cell.answers ? `答了 ${hover.cell.answers} 轮` : "没有练习"}
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-faint">
        <span>
          {days && stats.activeDays > 0
            ? `过去一年练了 ${stats.activeDays} 天。点有颜色的格子看那天练了什么。`
            : days
              ? "开始第一场面试后，这里会亮起来。"
              : "正在读取…"}
        </span>
        <span className="flex items-center gap-1" aria-hidden="true">
          少
          {levelClass.map((name) => (
            <span key={name} className={`size-[11px] rounded-[2px] ${name}`} />
          ))}
          多
        </span>
      </div>

      <div
        className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out ${chosen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0"}`}
      >
        <div className="overflow-hidden">{chosen && <DayDetail day={chosen} />}</div>
      </div>
    </section>
  );
}
