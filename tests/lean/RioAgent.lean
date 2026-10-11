/- Behavioral specification for rio.agent.session and owner. This models committed
   decisions, not SQLite fsync, JSON parsing, or operating-system containment. -/
namespace RioAgent

inductive Phase where
  | queued | started | finished
  deriving DecidableEq, Repr

structure Execution where
  phase : Phase
  unknown : Bool := false
  deriving DecidableEq, Repr

def recover (e : Execution) : Execution :=
  if e.phase == .started then ⟨.finished, true⟩ else e

theorem interrupted_never_requeues :
    recover ⟨.started, false⟩ = ⟨.finished, true⟩ := by rfl

theorem recovery_idempotent (e : Execution) : recover (recover e) = recover e := by
  cases e with
  | mk phase unknown => cases phase <;> simp [recover]

structure Cell where
  id : Nat
  previous : Option Nat := none
  deriving DecidableEq, Repr

def successor (old : Cell) (next : Nat) : Cell := ⟨next, some old.id⟩

/-- A step op names Cells by id; the runtime assigns ids, lineage, and positions. -/
inductive Op where
  | add | replace (id : Nat) | remove (id : Nat)
  deriving DecidableEq, Repr

def Op.target : Op → Option Nat
  | .add => none
  | .replace id | .remove id => some id

/-- Each add or replace takes the next id, in op order. -/
def allocate (next : Nat) : List Op → List Nat
  | [] => []
  | .remove _ :: ops => allocate next ops
  | _ :: ops => next :: allocate (next + 1) ops

theorem ids_follow_op_order :
    allocate 5 [.remove 1, .add, .replace 3, .add] = [5, 6, 7] := rfl

/-- A replacement keeps its position and points to the old id. -/
def replaceId (cells : List Cell) (id next : Nat) : List Cell :=
  cells.map fun c => if c.id == id then successor c next else c

theorem replacement_preserves_position :
    replaceId [⟨1, none⟩, ⟨2, none⟩] 1 5 = [⟨5, some 1⟩, ⟨2, none⟩] := rfl

/-- Removal is by id, so op order does not matter. -/
def removeIds (cells : List Cell) (ids : List Nat) : List Cell :=
  cells.filter fun c => c.id ∉ ids

theorem removal_order_is_irrelevant (cells : List Cell) (a b : List Nat) :
    removeIds cells (a ++ b) = removeIds cells (b ++ a) := by
  simp [removeIds, or_comm]

/-- Ops may target only Cells that existed before the step, each at most once. -/
def admissible (existing : List Nat) (ops : List Op) : Bool :=
  let targets := ops.filterMap Op.target
  targets.Nodup && targets.all (· ∈ existing)

theorem second_op_on_a_cell_is_rejected :
    admissible [1, 2] [.replace 1, .remove 1] = false := by decide

theorem cell_added_in_step_is_not_targetable (existing : List Nat) (next k : Nat)
    (h : ∀ x ∈ existing, x < next) : admissible existing [.replace (next + k)] = false := by
  have : next + k ∉ existing := fun m => by have := h _ m; omega
  simp [admissible, Op.target, this]

/-- The runtime, not the model, checks the revision a turn was prepared from. -/
def acceptsTurn (prepared current : Nat) : Bool := prepared == current

theorem stale_turn_is_rejected (prepared current : Nat) (h : prepared ≠ current) :
    acceptsTurn prepared current = false := by simp [acceptsTurn, h]

/-- Notes are built in; callers extend the agent with named runnable kinds. -/
inductive Kind where
  | note
  | runnable (name : String)
  deriving DecidableEq, Repr

/-- Admission accepts a note, or a runnable kind the caller registered. -/
def admits (registered : List String) : Kind → Bool
  | .note => true
  | .runnable name => registered.contains name

theorem note_only_agent_admits_only_notes (k : Kind) :
    admits [] k = true ↔ k = .note := by
  cases k <;> simp [admits]

theorem notes_need_no_registration (registered : List String) :
    admits registered .note = true := rfl

/-- The step schema offers note plus the registered kinds, in one shared Cell shape. -/
def kindEnum (registered : List String) : List String := "note" :: registered

theorem note_only_schema_offers_only_note : kindEnum [] = ["note"] := rfl

/-- A field name (paired with its JSON type) must mean the same thing in every kind. -/
def fieldsAgree (fields : List (String × String)) : Bool :=
  fields.all fun (f, t) => fields.all fun (g, u) => f != g || t == u

theorem redefined_field_is_rejected :
    fieldsAgree [("text", "string"), ("text", "integer")] = false := by decide

/-- Runnable Cells request one execution; notes never run. -/
def runs : Kind → Bool
  | .note => false
  | .runnable _ => true

def requested (cells : List (Nat × Kind)) : List Nat :=
  (cells.filter (runs ·.2)).map (·.1)

theorem notes_request_no_execution (ids : List Nat) :
    requested (ids.map (·, .note)) = [] := by
  induction ids <;> simp_all [requested, runs]

theorem every_runnable_kind_requests_alike (id : Nat) (a b : String) :
    requested [(id, .runnable a)] = requested [(id, .runnable b)] := rfl

structure Journal where
  accepted : List Nat := []
  executions : List Nat := []
  deriving DecidableEq, Repr

def accept (j : Journal) (turn : Nat) (codeIds : List Nat) : Journal :=
  if turn ∈ j.accepted then j
  else ⟨turn :: j.accepted, j.executions ++ codeIds⟩

theorem duplicate_turn_has_no_effect (j : Journal) (turn : Nat) (ids : List Nat) :
    accept (accept j turn ids) turn ids = accept j turn ids := by
  by_cases h : turn ∈ j.accepted <;> simp [accept, h]

def consumePause (pause : Option Nat) (delivered : List Nat) : Option Nat :=
  match pause with
  | none => none
  | some id => if id ∈ delivered then none else pause

theorem unrelated_response_preserves_pause (id : Nat) (delivered : List Nat)
    (h : id ∉ delivered) : consumePause (some id) delivered = some id := by
  simp [consumePause, h]

def canDispatch (terminal paused busy : Bool) : Bool :=
  !terminal && !paused && !busy

theorem terminal_never_dispatches (paused busy : Bool) :
    canDispatch true paused busy = false := by simp [canDispatch]

def canSucceed (pending unknown unread failedValidator : Bool) : Bool :=
  !pending && !unknown && !unread && !failedValidator

theorem unknown_blocks_success (pending unread failed : Bool) :
    canSucceed pending true unread failed = false := by simp [canSucceed]

theorem unread_blocks_success (pending unknown failed : Bool) :
    canSucceed pending unknown true failed = false := by simp [canSucceed]

/-- A successful completion waits while work is queued; a pause or empty queue wakes. -/
def wakesModel (execution pending paused : Bool) : Bool :=
  !execution || !pending || paused

theorem completion_with_queued_work_does_not_wake :
    wakesModel true true false = false := rfl

theorem pause_wakes_with_queued_work (execution : Bool) :
    wakesModel execution true true = true := by simp [wakesModel]

theorem drained_queue_wakes (execution paused : Bool) :
    wakesModel execution false paused = true := by simp [wakesModel]

theorem non_execution_input_wakes (pending paused : Bool) :
    wakesModel false pending paused = true := by simp [wakesModel]

def fireTimer (fired expired : Bool) : Bool × Nat :=
  if !fired && expired then (true, 1) else (fired, 0)

theorem expired_timer_is_not_redelivered : fireTimer (fireTimer false true).1 true = (true, 0) := by
  rfl

/-- The run loop sleeps until its nearest deadline: the retry time or an unfired timer.
   Commits and the model reply wake it earlier. -/
def wakeAt (retry : Nat) : List Nat → Nat
  | [] => retry
  | t :: ts => min t (wakeAt retry ts)

theorem wake_not_after_retry (retry : Nat) (timers : List Nat) : wakeAt retry timers ≤ retry := by
  induction timers with
  | nil => simp [wakeAt]
  | cons t ts ih => exact Nat.le_trans (Nat.min_le_right _ _) ih

theorem wake_not_after_timer (retry t : Nat) (timers : List Nat) (h : t ∈ timers) :
    wakeAt retry timers ≤ t := by
  induction timers with
  | nil => cases h
  | cons u us ih =>
    cases h with
    | head => exact Nat.min_le_left _ _
    | tail _ h => exact Nat.le_trans (Nat.min_le_right _ _) (ih h)

/-- Long output keeps its tail; the full output stays in a file. -/
def keepTail (limit : Nat) (output : List α) : List α := output.drop (output.length - limit)

theorem kept_tail_ends_output (limit : Nat) (output : List α) :
    output.take (output.length - limit) ++ keepTail limit output = output :=
  List.take_append_drop _ _

theorem short_output_is_whole (limit : Nat) (output : List α) (h : output.length ≤ limit) :
    keepTail limit output = output := by
  simp [keepTail, Nat.sub_eq_zero_of_le h]

/-- An explicit --agents-md path wins; otherwise the working directory's AGENTS.md. -/
def resolveAgentsMd (explicit cwdFile : Option String) : Option String :=
  explicit <|> cwdFile

theorem explicit_agents_md_wins (path : String) (cwdFile : Option String) :
    resolveAgentsMd (some path) cwdFile = some path := rfl

theorem cwd_agents_md_is_default (cwdFile : Option String) :
    resolveAgentsMd none cwdFile = cwdFile := rfl

/-- Instructions are fixed at Session creation and appended to the protocol prompt. -/
def systemPrompt (protocol instructions : String) : String := protocol ++ instructions

theorem no_instructions_keeps_protocol (protocol : String) :
    systemPrompt protocol "" = protocol := by simp [systemPrompt]

end RioAgent
