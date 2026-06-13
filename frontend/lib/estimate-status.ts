/** How often the Stage 4 questions page should poll the estimate, given its
 *  current status and whether the user has just submitted answers.
 *
 *  Returns a millisecond interval, or `false` to stop polling. The subtlety: while
 *  `awaiting_answers` we normally STOP polling (we're waiting on the user), but once
 *  they submit we must keep polling to catch the Pass 2 → completed transition — the
 *  backend resumes Pass 2 in the background and the page redirects on `completed`.
 *  `resuming` covers the brief window after submit where the status is still
 *  `awaiting_answers` before the backend flips it to `pass_2_running`.
 */
export function questionsPollInterval(
  status: string | undefined,
  resuming: boolean,
): number | false {
  if (!status) return 1500;
  switch (status) {
    case "completed":
    case "failed":
      return false; // terminal — stop polling
    case "pending":
    case "pass_1_running":
    case "pass_2_running":
    case "synthesizing":
      return 1500; // any in-progress state — keep polling
    case "awaiting_answers":
      return resuming ? 1500 : false; // poll only after the user has submitted
    default:
      return 1500; // unknown status — poll to be safe
  }
}
