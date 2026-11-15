import { describe, expect, it } from "vitest";
import {
  buildCalendar,
  levelOf,
  localToday,
  maxOffset,
  practiceStats,
  weeksFor,
  type PracticeDay,
} from "./practice";

function day(date: string, answers: number, ids: string[] = ["i1"]): PracticeDay {
  return {
    date,
    answers,
    sessions: ids.map((id) => ({
      interview_id: id,
      job_title: "",
      status: "completed",
      answers,
    })),
  };
}

describe("buildCalendar", () => {
  // 2026-11-15 是周日
  const calendar = buildCalendar([day("2026-11-10", 4), day("2026-11-15", 12)], "2026-11-15");

  it("画 53 周，每列周一到周日，最后一列含今天", () => {
    expect(calendar.weeks).toHaveLength(53);
    const last = calendar.weeks.at(-1)!;
    expect(last.map((cell) => cell.date)).toEqual([
      "2026-11-09",
      "2026-11-10",
      "2026-11-11",
      "2026-11-12",
      "2026-11-13",
      "2026-11-14",
      "2026-11-15",
    ]);
    expect(calendar.weeks[0]![0]!.date).toBe("2025-11-10");
  });

  it("按回答数标深浅，今天之后的格子标为未来", () => {
    const last = calendar.weeks.at(-1)!;
    expect(last[1]).toMatchObject({ answers: 4, level: 2 });
    expect(last[6]).toMatchObject({ answers: 12, level: 4, future: false });
    const midweek = buildCalendar([], "2026-11-12").weeks.at(-1)!;
    expect(midweek.map((cell) => cell.future)).toEqual([
      false,
      false,
      false,
      false,
      true,
      true,
      true,
    ]);
  });

  it("月份标签落在每月第一次出现的那一列，太挤的第一个不标", () => {
    // 第一列是 2025-11-10，到 12 月还有 3 列，保留
    expect(calendar.months[0]).toEqual({ column: 0, label: "11月" });
    expect(calendar.months[1]).toEqual({ column: 3, label: "12月" });
    expect(calendar.months.at(-1)).toEqual({ column: 51, label: "11月" });
    // 第一列是 2025-11-24，下一列就是 12 月，第一个 11 月不标
    expect(buildCalendar([], "2026-11-26").months[0]).toEqual({ column: 1, label: "12月" });
  });
});

describe("buildCalendar 翻页和宽度", () => {
  it("offset 往前翻整周，最后一列是若干周以前", () => {
    const page = buildCalendar([], "2026-11-15", 12, 12);
    expect(page.weeks).toHaveLength(12);
    expect(page.weeks.at(-1)![0]!.date).toBe("2026-08-17");
    expect(page.weeks.at(-1)!.every((cell) => !cell.future)).toBe(true);
  });

  it("按宽度算周数：手机约 12 周，电脑约 30 周，太窄也至少 8 周", () => {
    expect(weeksFor(343)).toBe(12);
    expect(weeksFor(832)).toBe(31);
    expect(weeksFor(120)).toBe(8);
    expect(weeksFor(5000)).toBe(53);
    expect(maxOffset(12)).toBe(41);
    expect(maxOffset(53)).toBe(0);
  });
});

describe("levelOf", () => {
  it.each([
    [0, 0],
    [1, 1],
    [2, 1],
    [3, 2],
    [5, 2],
    [6, 3],
    [9, 3],
    [10, 4],
  ])("%i 轮 → %i 级", (answers, level) => {
    expect(levelOf(answers)).toBe(level);
  });
});

describe("practiceStats", () => {
  const days = [
    day("2026-10-01", 3, ["a"]),
    day("2026-10-02", 3, ["a"]),
    day("2026-10-03", 3, ["b"]),
    day("2026-11-13", 2, ["c"]),
    day("2026-11-14", 5, ["c", "d"]),
  ];

  it("今天还没练时，连续天数算到昨天", () => {
    expect(practiceStats(days, "2026-11-15")).toEqual({
      currentStreak: 2,
      longestStreak: 3,
      activeDays: 5,
      monthSessions: 2,
    });
  });

  it("隔了一天没练就断", () => {
    expect(practiceStats(days, "2026-11-16").currentStreak).toBe(0);
  });

  it("跨月份的连续也算", () => {
    const stats = practiceStats([day("2026-10-31", 1), day("2026-11-01", 1)], "2026-11-01");
    expect(stats.currentStreak).toBe(2);
  });
});

it("localToday 用本地日期", () => {
  expect(localToday(new Date(2026, 0, 5, 23, 59))).toBe("2026-01-05");
});
