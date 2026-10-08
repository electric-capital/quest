/**
 * Help text under the routine Prompt field (Routine Settings and New Routine).
 * Every run is a fresh chat whose files stay in that run's Chat Files, so a
 * routine whose runs build on each other has to name the project space in its
 * prompt (devplan 00009 section 10.4).
 */
export const ROUTINE_PROMPT_FILES_HINT =
  "Each run is a fresh chat, and its files stay in that run's chat. If runs build on each other, "
  + 'have the prompt read and write the project space explicitly, e.g. "read proj://state.json, '
  + 'write proj://reports/weekly.md".';
