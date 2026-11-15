// 纸面风格的通用样式：平面、细线、小圆角；主按钮用墨色，强调色留给选中和进度
const buttonBase =
  "inline-flex max-w-full cursor-pointer items-center justify-center gap-2 rounded-md px-4 py-2 text-center text-sm font-medium transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-45 disabled:pointer-events-none";

export const buttonPrimary = `${buttonBase} bg-ink text-paper hover:bg-ink-soft`;
export const buttonSecondary = `${buttonBase} border border-line bg-transparent text-ink hover:bg-wash`;
export const buttonDanger = `${buttonBase} border border-danger-line bg-transparent text-danger hover:bg-danger-wash`;

export const panel = "rounded-lg border border-line bg-sheet p-6 sm:p-8";
export const field =
  "flex min-w-0 flex-col gap-1.5 [&>span]:text-xs [&>span]:text-muted";
export const input =
  "w-full min-w-0 rounded-md border border-line bg-sheet px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-accent focus:outline-hidden transition-colors disabled:bg-wash disabled:text-faint aria-invalid:border-danger";
