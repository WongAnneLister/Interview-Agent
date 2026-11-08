const buttonBase =
  "inline-flex max-w-full cursor-pointer items-center justify-center gap-2 rounded-[9px] border px-[18px] py-[9px] text-center text-sm leading-normal font-semibold hover:brightness-[0.93] disabled:hover:brightness-100 disabled:cursor-not-allowed disabled:opacity-45";

export const buttonPrimary = `${buttonBase} border-accent bg-accent text-white`;
export const buttonSecondary = `${buttonBase} border-[#cbd8d1] bg-white text-ink`;
export const buttonDanger = `${buttonBase} border-[#e8c7c0] bg-white text-[#a43025]`;

export const panel = "rounded-2xl border border-border bg-white p-5 sm:p-7";
export const field =
  "flex min-w-0 flex-col gap-1.5 [&>span]:text-sm [&>span]:font-semibold";
export const input =
  "w-full min-w-0 rounded-lg border border-[#bdcec6] bg-white px-3 py-2.5 text-ink disabled:bg-[#f5f7f4] aria-invalid:border-[#b64032]";
