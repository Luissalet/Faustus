/** A paste event is user-authorized clipboard data; no global clipboard read. */
export function clipboardFiles(data: Pick<DataTransfer, 'items' | 'files'>): File[] {
  const items: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== 'file') continue;
    try {
      const file = item.getAsFile();
      if (file) items.push(file.type || !item.type ? file : new File([file], file.name, {type:item.type, lastModified:file.lastModified}));
    } catch { /* A files fallback is available in some browsers. */ }
  }
  // These are two representations of the same transfer, not two attachments.
  return items.length ? items : Array.from(data.files ?? []);
}

export function insertPastedText(value: string, start: number, end: number, text: string) {
  return {value: value.slice(0, start) + text + value.slice(end), caret: start + text.length};
}
