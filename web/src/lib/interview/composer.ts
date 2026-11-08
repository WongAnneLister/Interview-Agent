export function shouldSubmit(event: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
  keyCode: number;
}): boolean {
  return event.key === "Enter" && !event.shiftKey &&
    !event.isComposing && event.keyCode !== 229;
}
