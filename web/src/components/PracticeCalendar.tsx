"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { listPracticeDays } from "@/lib/api/client";
import { interviewStatusLabels } from "@/lib/labels";
import {
  buildCalendar,
  localToday,
  maxOffset,
  practiceStats,
  weeksFor,
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
const LABEL_WIDTH = 28; // 左边“一 三 五 日”那一列

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

function rangeLabel(first: string, last: string): string {
  const short = (date: string) => dateLabel(date).replace(/ 周.$/, "");
  return `${short(first)} – ${short(last)}`;
}

export function PracticeCalendar() {
  const [days, setDays] = useState<PracticeDay[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [hover, setHover] = useState<{ cell: CalendarCell; x: number; y: number } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [width, setWidth] = useState(0);
  const [offset, setOffset] = useState(0);
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

  // 按可用宽度决定显示几周：手机三个月左右，电脑七个月左右，格子都在 22px 上下
  useEffect(() => {
    const el = board.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.round(entry.contentRect.width));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [failed]);

  const weeks = width ? weeksFor(width, LABEL_WIDTH) : 0;
  const page = Math.min(offset, maxOffset(weeks));
  const calendar = useMemo(
    () => buildCalendar(days ?? [], today, weeks || 1, page),
    [days, today, weeks, page],
  );
  const stats = useMemo(() => practiceStats(days ?? [], today), [days, today]);
  const chosen = days?.find((day) => day.date === selected) ?? null;
  const firstDate = calendar.weeks[0]?.[0]?.date;
  const lastWeek = calendar.weeks.at(-1) ?? [];
  const lastDate = lastWeek.filter((cell) => !cell.future).at(-1)?.date;

  if (failed) return null; // 只是锦上添花，读不到就不显示，不打扰准备面试

  function show(cell: CalendarCell, target: HTMLElement) {
    // 提示框约 150px 宽、以格子为中心：靠两边时往里收，不超出日历
    const x = Math.min(
      Math.max(target.offsetLeft + target.offsetWidth / 2, 76),
      width - 76,
    );
    setHover({ cell, x, y: target.offsetTop });
  }

  function move(step: number) {
    setHover(null);
    setOffset(Math.min(maxOffset(weeks), Math.max(0, page + step)));
  }

  const columns = { gridTemplateColumns: `${LABEL_WIDTH - 4}px repeat(${weeks || 1}, minmax(0, 1fr))` };

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

      <div className="flex items-center justify-between gap-3 text-sm text-muted">
        <button
          type="button"
          className="cursor-pointer px-1 hover:text-ink disabled:cursor-not-allowed disabled:text-faint"
          disabled={!days || page >= maxOffset(weeks)}
          onClick={() => move(weeks)}
        >
          ‹ 更早
        </button>
        <span className="font-serif text-ink tabular-nums">
          {firstDate && lastDate ? rangeLabel(firstDate, lastDate) : ""}
        </span>
        <button
          type="button"
          className="cursor-pointer px-1 hover:text-ink disabled:cursor-not-allowed disabled:text-faint"
          disabled={!days || page === 0}
          onClick={() => move(-weeks)}
        >
          更近 ›
        </button>
      </div>

      <div
        ref={board}
        className="relative"
        onMouseLeave={() => setHover(null)}
        style={{ minHeight: width ? undefined : 200 }}
      >
        {days && weeks > 0 && (
          <div key={`${weeks}-${page}`} className="flex flex-col gap-1.5">
            <div className="grid gap-1 text-[11px] text-faint" style={columns} aria-hidden="true">
              {calendar.months.map((month) => (
                <span
                  key={month.column}
                  className="whitespace-nowrap"
                  style={{ gridColumn: month.column + 2, gridRow: 1 }}
                >
                  {month.label}
                </span>
              ))}
            </div>
            <div className="grid grid-flow-col grid-rows-7 gap-1" style={columns}>
              {weekdays.map((name, index) => (
                <span
                  key={name}
                  aria-hidden="true"
                  className="flex items-center text-[11px] text-faint"
                  style={{ gridColumn: 1, gridRow: index + 1 }}
                >
                  {index % 2 === 0 ? name : ""}
                </span>
              ))}
              {calendar.weeks.flatMap((week, column) =>
                week.map((cell, row) => {
                  const place = { gridColumn: column + 2, gridRow: row + 1 };
                  if (cell.future) return <span key={cell.date} style={place} />;
                  const isToday = cell.date === today;
                  const motion = isToday ? "cell-today" : cell.level > 0 ? "cell-pop" : "cell-in";
                  const style = {
                    ...place,
                    animationDelay: `${Math.min(700, (column + row) * 22)}ms`,
                  };
                  const common = `aspect-square w-full rounded-[4px] ${levelClass[cell.level]} ${motion}`;
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
                      className={`${common} cursor-pointer transition-transform duration-150 hover:scale-125 focus-visible:scale-125 aria-pressed:outline-2 aria-pressed:outline-offset-2 aria-pressed:outline-ink`}
                      style={style}
                      onMouseEnter={(event) => show(cell, event.currentTarget)}
                      onFocus={(event) => show(cell, event.currentTarget)}
                      onBlur={() => setHover(null)}
                      onClick={() =>
                        setSelected((value) => (value === cell.date ? null : cell.date))
                      }
                    />
                  );
                }),
              )}
            </div>
          </div>
        )}

        {hover && (
          <div
            key={hover.cell.date}
            role="tooltip"
            className="tip-in pointer-events-none absolute z-10 rounded-md bg-ink px-2.5 py-1.5 text-xs whitespace-nowrap text-paper"
            style={{ left: hover.x, top: hover.y - 36 }}
          >
            {dateLabel(hover.cell.date)}
            <span className="ml-1.5 text-paper/70">
              {hover.cell.answers ? `答了 ${hover.cell.answers} 轮` : "没有练习"}
            </span>
          </div>
        )}
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
            <span key={name} className={`size-3 rounded-[3px] ${name}`} />
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
