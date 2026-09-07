const STOP_WORDS: Record<string, string> = {
  complete: 'finished',
  complete_unverified: 'finished unverified',
  rounds: 'ran out of rounds',
  budget: 'ran out of budget',
  stopped: 'stopped',
  error: 'error',
  asked_user: 'asked you',
  awaiting_user: 'Waiting for your permission',
};

/** Stored counts stay intact; a gate pause is not presented as a tool crash. */
export function harnessOutcomeWords(stopReason: string, permissionAnswered = false) {
  const gatePause = stopReason === 'awaiting_user';
  return {
    stop: gatePause && permissionAnswered ? 'Permission answered' : STOP_WORDS[stopReason] ?? stopReason,
    failed: gatePause ? '{n} incomplete' : '{n} failed',
  };
}
