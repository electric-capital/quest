/**
 * The "Routines" section inside a drilled project: one row per routine with
 * its schedule indicator, settings gear and run button.
 */

import type { Routine } from '../../api/types';
import { ClockIcon, GearIcon, PlayIcon, PlusIcon } from './icons';

interface RoutinesSectionProps {
  routines: Routine[];
  // A routine run (conversation creation) is in flight for this project.
  running: boolean;
  onNewRoutine: () => void;
  onOpenSettings: (routine: Routine) => void;
  onRun: (routine: Routine) => void;
}

export function RoutinesSection({ routines, running, onNewRoutine, onOpenSettings, onRun }: RoutinesSectionProps) {
  return (
    <div className="routines-section">
      <div className="section-header">
        <div className="section-label">Routines</div>
        <button
          className="section-add-button"
          onClick={onNewRoutine}
          title="New Routine"
          aria-label="New Routine"
        >
          <PlusIcon />
        </button>
      </div>
      {routines.map((routine) => (
        <div key={routine.id} className="routine-item">
          <span className="routine-name">{routine.name}</span>
          {routine.schedule?.is_enabled && (
            <span className="routine-schedule-indicator" title="Scheduled">
              <ClockIcon />
            </span>
          )}
          <button
            className="routine-settings-button"
            onClick={() => onOpenSettings(routine)}
            title={`Settings for "${routine.name}"`}
          >
            <GearIcon size={12} />
          </button>
          <button
            className="routine-play-button"
            onClick={() => onRun(routine)}
            title={`Run "${routine.name}"`}
            disabled={running}
          >
            <PlayIcon />
          </button>
        </div>
      ))}
    </div>
  );
}
