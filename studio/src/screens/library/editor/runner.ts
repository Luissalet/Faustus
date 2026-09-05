/** What the tool pane can ask the editor screen to run on the server. */
export interface Runner {
  inpaint(kind: 'generate' | 'remove' | 'outpaint'): Promise<void>;
  removeBg(): Promise<void>;
  sharpen(): Promise<void>;
  denoise(): Promise<void>;
  enhanceFace(): Promise<void>;
  harmonize(): Promise<void>;
  style(): Promise<void>;
  samFind(): Promise<void>;
  upscaleAi(): Promise<void>;
  upscaleLocal(factor: number): void;
  autoMatch(): Promise<void>;
}
