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

def replaceHead (cells : List Cell) (next : Nat) : List Cell :=
  match cells with
  | [] => []
  | head :: tail => successor head next :: tail

theorem replacement_preserves_position (a b : Cell) (next : Nat) :
    replaceHead [a,b] next = [⟨next, some a.id⟩, b] := by rfl

/-- /cells/N addresses a position; a rejected id-as-index names the id's position. -/
def positionOf (cells : List Cell) (id : Nat) : Option Nat :=
  cells.findIdx? (·.id == id)

def indexHint (cells : List Cell) (n : Nat) : Option Nat :=
  if n < cells.length then none else positionOf cells n

theorem in_range_index_needs_no_hint (cells : List Cell) (n : Nat) (h : n < cells.length) :
    indexHint cells n = none := by simp [indexHint, h]

theorem id_used_as_index_points_to_position :
    indexHint [⟨2, none⟩] 2 = some 0 := by rfl

/-- A replacement must name the Cell at its position as predecessor. -/
def admitsReplacement (old next : Cell) : Bool := next.previous == some old.id

theorem successor_is_admitted (old : Cell) (next : Nat) :
    admitsReplacement old (successor old next) = true := by simp [admitsReplacement, successor]

theorem null_predecessor_is_stale (old : Cell) (next : Nat) :
    admitsReplacement old ⟨next, none⟩ = false := by simp [admitsReplacement]

/-- Replace/remove may target only Cells that existed before the patch (id < next). -/
def changeable (cellId nextId : Nat) : Bool := cellId < nextId

theorem cell_added_in_patch_is_not_changeable (nextId k : Nat) :
    changeable (nextId + k) nextId = false := by simp [changeable]

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
