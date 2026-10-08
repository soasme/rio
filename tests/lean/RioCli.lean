/- Behavioral model of the human durable transcript. This specifies publication
   and suffix selection, not Python execution, terminal layout, or SQLite commits. -/
namespace RioCli

structure Cell where
  id : Nat
  body : String
  deriving DecidableEq, Repr

inductive Event where
  | accepted (newCells : List Cell)
  | proposed (cells : List Cell)
  | snapshot (cells : List Cell)
  | rejected
  deriving DecidableEq, Repr

def visibleCells : Event → List String
  | .accepted cells => cells.map Cell.body
  | _ => []

theorem accepted_source_is_visible (id : Nat) (source : String) :
    visibleCells (.accepted [⟨id, source⟩]) = [source] := by rfl

-- Command presentation joins quoted argv; quoting is supplied by Python shlex.
-- This models visibility, not shell parsing or execution.
theorem accepted_command_is_visible (id : Nat) (argv : List String)
    (quote : String → String) :
    visibleCells (.accepted [⟨id, String.intercalate " " (argv.map quote)⟩]) =
      [String.intercalate " " (argv.map quote)] := by rfl

theorem cell_ids_do_not_affect_content (first second : Nat) (body : String) :
    visibleCells (.accepted [⟨first, body⟩]) =
      visibleCells (.accepted [⟨second, body⟩]) := by rfl

theorem proposals_are_not_published (cells : List Cell) :
    visibleCells (.proposed cells) = [] := by rfl

theorem state_snapshots_do_not_repeat_cells (cells : List Cell) :
    visibleCells (.snapshot cells) = [] := by rfl

theorem rejected_patch_has_no_cells : visibleCells .rejected = [] := by rfl

-- Each stream tracks its own previous length. Durable snapshots only append.
def outputDelta (displayed : Nat) (snapshot : List Char) : List Char :=
  snapshot.drop displayed

theorem append_prints_only_new_output (old new : List Char) :
    outputDelta old.length (old ++ new) = new := by
  simp [outputDelta]

theorem repeated_snapshot_prints_nothing (snapshot : List Char) :
    outputDelta snapshot.length snapshot = [] := by
  simp [outputDelta]

-- OSC 7501 program status. The environment override wins over terminal detection.
def statusEnabled (override : Option Bool) (terminal : Bool) : Bool :=
  override.getD terminal

theorem override_forces_status (on terminal : Bool) :
    statusEnabled (some on) terminal = on := by rfl

theorem detection_without_override (terminal : Bool) :
    statusEnabled none terminal = terminal := by rfl

inductive Outcome where
  | succeeded
  | failed
  | interrupted
  deriving DecidableEq, Repr

-- A run reports `working`, then exactly one final state.
def reports : Outcome → List String
  | .succeeded => ["working", "done"]
  | .failed => ["working", "error"]
  | .interrupted => ["working", "idle"]

theorem run_starts_working (o : Outcome) : (reports o).head? = some "working" := by
  cases o <;> rfl

theorem run_reports_two_states (o : Outcome) : (reports o).length = 2 := by
  cases o <;> rfl

theorem interrupt_is_not_failure : reports .interrupted ≠ reports .failed := by decide

end RioCli
