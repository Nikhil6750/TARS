/** Legacy presentation-only status shape for optional HUD indicators. */
export interface WakeWordStatusInfo {
  isActive: boolean;
  engine?: string;
  engineLabel?: string;
  targetPhrase?: string;
}
