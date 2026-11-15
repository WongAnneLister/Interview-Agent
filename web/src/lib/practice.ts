import type { components } from "./api/schema";

export type PracticeDay = components["schemas"]["PracticeDay"];
export type CalendarCell = {
  date: string; // YYYY-MM-DD，当地日期
  answers: number;
  level: 0 | 1 | 2 | 3 | 4;
  day: PracticeDay | null;
  future: boolean;
};
export type Calendar = {
  weeks: CalendarCell[][]; // 每列一周，周一在上
  months: Array<{ column: number; label: string }>;
};

const DAY_MS = 86_400_000;

/** 用 UTC 的日期运算处理“当地日期字符串”，避免夏令时把一天算成 23 或 25 小时。 */
function toUtc(date: string): number {
  return Date.parse(`${date}T00:00:00Z`);
}

function format(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

/** 浏览器所在时区的今天，YYYY-MM-DD。 */
export function localToday(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** 每天答了几轮 → 深浅 1–4：1–2 轮、3–5 轮、6–9 轮、10 轮以上（一场 15 分钟面试大约 6–10 轮）。 */
export function levelOf(answers: number): CalendarCell["level"] {
  if (answers <= 0) return 0;
  if (answers <= 2) return 1;
  if (answers <= 5) return 2;
  if (answers <= 9) return 3;
  return 4;
}

export function buildCalendar(days: PracticeDay[], today: string, weeks = 53): Calendar {
  const byDate = new Map(days.map((day) => [day.date, day]));
  const todayMs = toUtc(today);
  const weekday = (new Date(todayMs).getUTCDay() + 6) % 7; // 周一 = 0
  const start = todayMs - (weekday + (weeks - 1) * 7) * DAY_MS;
  const columns: CalendarCell[][] = [];
  const months: Calendar["months"] = [];
  let lastMonth = -1;
  for (let column = 0; column < weeks; column += 1) {
    const cells: CalendarCell[] = [];
    for (let row = 0; row < 7; row += 1) {
      const ms = start + (column * 7 + row) * DAY_MS;
      const date = format(ms);
      const day = byDate.get(date) ?? null;
      const answers = day?.answers ?? 0;
      cells.push({ date, answers, level: levelOf(answers), day, future: ms > todayMs });
    }
    const month = new Date(start + column * 7 * DAY_MS).getUTCMonth();
    if (month !== lastMonth) {
      months.push({ column, label: `${month + 1}月` });
      lastMonth = month;
    }
    columns.push(cells);
  }
  // 第一列的月份只露出一两周时不标，免得和下个月的标签挤在一起
  const [first, second] = months;
  if (first && second && second.column - first.column < 3) months.shift();
  return { weeks: columns, months };
}

export type PracticeStats = {
  currentStreak: number; // 截至今天（今天还没练就截至昨天）连续练习的天数
  longestStreak: number;
  activeDays: number;
  monthSessions: number; // 本月练了几场（同一场跨天只算一次）
};

export function practiceStats(days: PracticeDay[], today: string): PracticeStats {
  const dates = new Set(days.filter((d) => d.answers > 0).map((d) => d.date));
  let longest = 0;
  let run = 0;
  let previous = Number.NaN;
  for (const date of [...dates].sort()) {
    const ms = toUtc(date);
    run = ms - previous === DAY_MS ? run + 1 : 1;
    longest = Math.max(longest, run);
    previous = ms;
  }
  let current = 0;
  let cursor = toUtc(today);
  if (!dates.has(today)) cursor -= DAY_MS;
  while (dates.has(format(cursor))) {
    current += 1;
    cursor -= DAY_MS;
  }
  const month = today.slice(0, 7);
  const sessions = new Set(
    days
      .filter((d) => d.date.startsWith(month))
      .flatMap((d) => d.sessions.map((s) => s.interview_id)),
  );
  return {
    currentStreak: current,
    longestStreak: longest,
    activeDays: dates.size,
    monthSessions: sessions.size,
  };
}
