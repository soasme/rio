/- Behavioral specification for rio.agent.session, ledger and owner. This models committed
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

/-- Cell ops name Cells by id; the runtime assigns ids, lineage, and positions.
   A control is an action, not a Cell: it never enters State. -/
inductive Op where
  | add | replace (id : Nat) | remove (id : Nat) | control
  deriving DecidableEq, Repr

def Op.target : Op → Option Nat
  | .add | .control => none
  | .replace id | .remove id => some id

/-- Each add or replace takes the next Cell id, in op order. -/
def allocate (next : Nat) : List Op → List Nat
  | [] => []
  | .remove _ :: ops | .control :: ops => allocate next ops
  | _ :: ops => next :: allocate (next + 1) ops

theorem ids_follow_op_order :
    allocate 5 [.remove 1, .add, .replace 3, .add] = [5, 6, 7] := rfl

theorem controls_take_no_cell_ids :
    allocate 5 [.control, .add, .control, .replace 3] = [5, 6] := rfl

/-- Controls take their own ids, counting on from the journaled controls. -/
def allocateControls (recorded : Nat) (ops : List Op) : List Nat :=
  (List.range (ops.filter (· == .control)).length).map (recorded + 1 + ·)

theorem control_ids_follow_recorded (recorded : Nat) (ops : List Op) :
    ∀ id ∈ allocateControls recorded ops, recorded < id := by
  intro id h
  simp [allocateControls] at h
  omega

theorem control_ids_are_distinct (recorded : Nat) (ops : List Op) :
    (allocateControls recorded ops).Nodup := by
  unfold allocateControls
  exact List.Pairwise.map _ (fun _ _ h => by omega) List.nodup_range

/-- Each control type requires its fields besides type and reason (state.CONTROLS). -/
inductive ControlType where
  | cancel | resolution | conclusion
  deriving DecidableEq, Repr

def ControlType.fields : ControlType → List String
  | .cancel => ["target_cell_id"]
  | .resolution => ["target_cell_id", "evidence_refs"]
  | .conclusion => ["result"]

/-- A control is admitted with exactly its type's fields. -/
def controlAdmits (t : ControlType) (given : List String) : Bool :=
  given.all (· ∈ "reason" :: t.fields) && ("reason" :: t.fields).all (· ∈ given)

theorem cancel_requires_target :
    controlAdmits .cancel ["reason"] = false := by decide

theorem conclusion_rejects_target :
    controlAdmits .conclusion ["reason", "result", "target_cell_id"] = false := by decide

theorem resolution_admits_its_fields :
    controlAdmits .resolution ["target_cell_id", "evidence_refs", "reason"] = true := by decide

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

/-- The runner decides whether to call the model, wait, or record invalid idle. -/
inductive IdleAction where
  | ready | wait | invalid | fail
  deriving DecidableEq, Repr

def idleAction (first unaccepted newInput wakeup : Bool) (idle limit : Nat) : IdleAction :=
  if first || unaccepted || newInput then .ready
  else if wakeup then .wait
  else if idle ≥ limit then .fail
  else .invalid

theorem unaccepted_turn_is_retried (first input wakeup : Bool) (idle limit : Nat) :
    idleAction first true input wakeup idle limit = .ready := by simp [idleAction]

theorem wakeup_prevents_invalid_idle (idle limit : Nat) :
    idleAction false false false true idle limit = .wait := rfl

theorem idle_budget_is_bounded (idle limit : Nat) (h : idle ≥ limit) :
    idleAction false false false false idle limit = .fail := by simp [idleAction, h]

/-- Inbox byte accounting adds one comma after the first observation. -/
def appendBytes (current message : Nat) (hasMessages : Bool) : Nat :=
  current + message + if hasMessages then 1 else 0

def admitBytes (current message limit : Nat) (hasMessages : Bool) : Nat :=
  let next := appendBytes current message hasMessages
  if next ≤ limit then next else current

theorem admitted_context_fits (current message limit : Nat) (hasMessages : Bool)
    (h : current ≤ limit) : admitBytes current message limit hasMessages ≤ limit := by
  simp only [admitBytes]
  split <;> assumption

theorem separator_can_exhaust_budget (current message : Nat) :
    admitBytes current message (current + message) true = current := by
  simp only [admitBytes, appendBytes, ↓reduceIte]
  split <;> omega

/-- A prepared turn shows each output stream up to a cap; the rest is counted as omitted. -/
def shownBytes (recorded cap : Nat) : Nat := min recorded cap

def omittedBytes (recorded cap : Nat) : Nat := recorded - shownBytes recorded cap

theorem shown_and_omitted_cover_record (recorded cap : Nat) :
    shownBytes recorded cap + omittedBytes recorded cap = recorded := by
  simp only [omittedBytes, shownBytes]
  omega

theorem stream_within_cap_is_whole (recorded cap : Nat) (h : recorded ≤ cap) :
    omittedBytes recorded cap = 0 := by
  simp only [omittedBytes, shownBytes]
  omega

/-- Streams within an equal share stay whole; the others share what is left equally. -/
def fill (whole : List Nat) (larger budget : Nat) : Nat :=
  whole.sum + larger * ((budget - whole.sum) / larger)

theorem fill_fits_budget (whole : List Nat) (larger budget : Nat) (h : whole.sum ≤ budget) :
    fill whole larger budget ≤ budget := by
  simp only [fill]
  have := Nat.mul_div_le (budget - whole.sum) larger
  omega

/-- Fall back from all shown records to outstanding work, then none; stop only if none fits. -/
def firstFit (limit : Nat) : List Nat → Option Nat
  | [] => none
  | bytes :: rest => if bytes ≤ limit then some bytes else firstFit limit rest

theorem first_fit_fits (limit bytes : Nat) :
    ∀ candidates, firstFit limit candidates = some bytes → bytes ≤ limit
  | [], h => by simp [firstFit] at h
  | b :: rest, h => by
    simp only [firstFit] at h
    split at h
    · cases h; assumption
    · exact first_fit_fits limit bytes rest h

theorem minimal_state_fits_without_stop (limit records outstanding minimal : Nat)
    (h : minimal ≤ limit) : (firstFit limit [records, outstanding, minimal]).isSome := by
  simp only [firstFit]
  by_cases h1 : records ≤ limit <;> by_cases h2 : outstanding ≤ limit <;> simp [h1, h2, h]

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

/-- Worker buffers, committed output, and validators keep byte tails.
   Finished worker output also applies a line limit and links to the full file. -/
def keepTail (limit : Nat) (output : List α) : List α := output.drop (output.length - limit)

theorem kept_tail_ends_output (limit : Nat) (output : List α) :
    output.take (output.length - limit) ++ keepTail limit output = output :=
  List.take_append_drop _ _

theorem short_output_is_whole (limit : Nat) (output : List α) (h : output.length ≤ limit) :
    keepTail limit output = output := by
  simp [keepTail, Nat.sub_eq_zero_of_le h]

theorem kept_tail_fits_budget (limit : Nat) (output : List α) :
    (keepTail limit output).length ≤ limit := by
  simp only [keepTail, List.length_drop]
  omega

theorem new_output_replaces_old_tail (limit : Nat) (old fresh : List α)
    (h : limit ≤ fresh.length) :
    keepTail limit (old ++ fresh) = keepTail limit fresh := by
  simp only [keepTail, List.length_append, List.drop_append]
  have hdrop : old.length ≤ old.length + fresh.length - limit := by omega
  rw [List.drop_eq_nil_iff.mpr hdrop]
  simp only [List.nil_append]
  congr 1
  omega

/-- Worker EOF without an exit record leaves execution uncertain. -/
inductive WorkerResult where
  | success | error | unknownExecution
  deriving DecidableEq, Repr

def workerResult : Option Int → WorkerResult
  | none => .unknownExecution
  | some code => if code == 0 then .success else .error

theorem worker_success_iff_zero (code : Option Int) :
    workerResult code = .success ↔ code = some 0 := by
  cases code with
  | none => simp [workerResult]
  | some code => simp [workerResult]

theorem worker_missing_exit_is_unknown : workerResult none = .unknownExecution := rfl

theorem worker_nonzero_exit_is_error (code : Int) (h : code ≠ 0) :
    workerResult (some code) = .error := by simp [workerResult, h]

/-- Admission retains allocation state; the next turn rebuilds runtime observations. -/
structure RuntimeSnapshot where
  nextId : Nat
  cells : List Nat
  inbox : List Nat
  usage : Nat
  deriving DecidableEq, Repr

def resetRuntime (runtime : RuntimeSnapshot) (added : Nat) : RuntimeSnapshot :=
  ⟨runtime.nextId + added, [], [], 0⟩

theorem admission_preserves_next_id (runtime : RuntimeSnapshot) (added : Nat) :
    (resetRuntime runtime added).nextId = runtime.nextId + added := rfl

theorem admission_clears_observations (runtime : RuntimeSnapshot) (added : Nat) :
    (resetRuntime runtime added).cells = [] ∧
    (resetRuntime runtime added).inbox = [] ∧
    (resetRuntime runtime added).usage = 0 := by simp [resetRuntime]

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

/-- Prepared context is immutable; later journal events replace only mutable turn fields. -/
structure JournalTurn where
  state : List Nat
  attempts : Nat
  response : Option String
  receipt : Option Nat
  deriving DecidableEq, Repr

inductive TurnDelta where
  | attempt
  | response (text : String)
  | reject
  | providerFailed
  | accept (receipt : Nat)

def applyTurnDelta (turn : JournalTurn) : TurnDelta → JournalTurn
  | .attempt => { turn with attempts := turn.attempts + 1, response := none }
  | .response text => { turn with response := some text }
  | .reject => { turn with response := none }
  -- A failed provider request refunds its attempt to the invalid-step budget.
  | .providerFailed => { turn with attempts := turn.attempts - 1, response := none }
  | .accept receipt => { turn with receipt := some receipt }

theorem turn_delta_preserves_prepared_context (turn : JournalTurn) (delta : TurnDelta) :
    (applyTurnDelta turn delta).state = turn.state := by cases delta <;> rfl

/-- `usage.turns` counts new prepared turns, not attempts or accepted steps.
    Model only the turn-budget branch; lifecycle, token, and storage checks are separate. -/
structure TurnBudget where
  turns : Nat
  pending : Bool
  deriving DecidableEq, Repr

def prepareWithinTurnBudget (budget : TurnBudget) (limit : Nat) : Option TurnBudget :=
  if budget.pending then some budget
  else if budget.turns < limit then
    some { turns := budget.turns + 1, pending := true }
  else none

theorem pending_turn_reused_at_turn_limit (turns limit : Nat) :
    prepareWithinTurnBudget ⟨turns, true⟩ limit = some ⟨turns, true⟩ := rfl

theorem new_turn_counts_once (turns limit : Nat) (h : turns < limit) :
    prepareWithinTurnBudget ⟨turns, false⟩ limit = some ⟨turns + 1, true⟩ := by
  simp [prepareWithinTurnBudget, h]

theorem turn_limit_blocks_new_turn (turns limit : Nat) (h : ¬ turns < limit) :
    prepareWithinTurnBudget ⟨turns, false⟩ limit = none := by
  simp [prepareWithinTurnBudget, h]

/-- Attempt, response, and rejection keep the turn pending; acceptance closes it. -/
def accountTurnDelta (budget : TurnBudget) : TurnDelta → TurnBudget
  | .accept _ => { budget with pending := false }
  | _ => budget

theorem turn_progress_does_not_count_turns (budget : TurnBudget) (delta : TurnDelta) :
    (accountTurnDelta budget delta).turns = budget.turns := by cases delta <;> rfl

theorem rejection_reuses_turn (turns limit : Nat) :
    prepareWithinTurnBudget (accountTurnDelta ⟨turns, true⟩ .reject) limit =
      some ⟨turns, true⟩ := rfl

theorem model_attempt_increments_attempts (turn : JournalTurn) :
    (applyTurnDelta turn .attempt).attempts = turn.attempts + 1 := rfl

theorem provider_failure_refunds_attempt (turn : JournalTurn) :
    (applyTurnDelta (applyTurnDelta turn .attempt) .providerFailed).attempts =
      turn.attempts := by simp [applyTurnDelta]

theorem saved_response_preserves_attempts (turn : JournalTurn) (response : String) :
    (applyTurnDelta turn (.response response)).attempts = turn.attempts := rfl

/-- Chunk concatenation before applying the output retention limit. -/
inductive OutputRecord where
  | snapshot (text : List Char)
  | chunk (text : List Char)

def applyOutput (old : List Char) : OutputRecord → List Char
  | .snapshot text => text
  | .chunk text => old ++ text

theorem chunks_equal_one_snapshot (old first second : List Char) :
    applyOutput (applyOutput old (.chunk first)) (.chunk second) =
      applyOutput old (.snapshot (old ++ first ++ second)) := rfl

theorem chunk_storage_is_linear (chunks : List (List Char)) :
    chunks.flatten.length = (chunks.map List.length).sum := by
  induction chunks with
  | nil => rfl
  | cons chunk rest ih => simp [ih]

/-- Missing completion metadata is an error; token limits precede tool use. -/
inductive StreamEnd where
  | error | length | toolUse | stop
  deriving DecidableEq, Repr

def streamEnd (finished limited tools : Bool) : StreamEnd :=
  if !finished then .error else if limited then .length else if tools then .toolUse else .stop

theorem missing_finish_never_succeeds (limited tools : Bool) :
    streamEnd false limited tools = .error := rfl

theorem token_limit_precedes_tool_use (tools : Bool) :
    streamEnd true true tools = .length := rfl

/-- Either conclusion ends the run and marks its control handled in one commit. -/
inductive Conclusion where
  | success | failure
  deriving DecidableEq, Repr

def conclusionCommits (_ : Conclusion) : List (Bool × Bool) :=
  [(true, true)]  -- (terminal, control handled)

theorem conclusion_is_one_commit (c : Conclusion) :
    (conclusionCommits c).length = 1 ∧ conclusionCommits c = [(true, true)] := by
  cases c <;> simp [conclusionCommits]

/-- Recovery, including terminal replay, checks POSIX before stopping any worker. -/
def recoverStops (posix : Bool) (workers : List Nat) : Option (List Nat) :=
  if posix then some workers else none

theorem non_posix_recovery_stops_nothing (workers : List Nat) :
    recoverStops false workers = none := rfl

/-- Only an explicit run stop becomes a failure conclusion; patch errors retry.
   Provider failures back off without a rejection; exhausting their budget is a run stop.
   Unexpected errors propagate without writing a conclusion or rejection. -/
inductive TurnError where
  | invalidPatch | providerFailure | runStopped | unexpected
  deriving DecidableEq, Repr

inductive TurnDisposition where
  | reject | backoff | fail | propagate
  deriving DecidableEq, Repr

def handleTurnError : TurnError → TurnDisposition
  | .invalidPatch => .reject
  | .providerFailure => .backoff
  | .runStopped => .fail
  | .unexpected => .propagate

theorem only_explicit_stop_concludes_failure (error : TurnError) :
    handleTurnError error = .fail ↔ error = .runStopped := by
  cases error <;> simp [handleTurnError]

theorem invalid_patch_remains_retryable :
    handleTurnError .invalidPatch = .reject := rfl

theorem provider_failure_is_not_a_rejection :
    handleTurnError .providerFailure ≠ .reject := by simp [handleTurnError]

/-- Provider failures stop the run only when final or at their own budget. -/
def providerFailureStops (retryable : Bool) (failures limit : Nat) : Bool :=
  !retryable || limit ≤ failures

theorem retryable_failure_under_budget_continues (failures limit : Nat) (h : failures < limit) :
    providerFailureStops true failures limit = false := by
  simp [providerFailureStops]; omega

theorem final_provider_error_stops (failures limit : Nat) :
    providerFailureStops false failures limit = true := rfl

theorem unexpected_error_propagates :
    handleTurnError .unexpected = .propagate := rfl

/-- The runtime ledger patches a full record, preserving fields not supplied by the caller. -/
abbrev LedgerRecord := String → Option String
abbrev RuntimeLedger := String × String → LedgerRecord

def patchRecord (record fields : LedgerRecord) : LedgerRecord :=
  fun field => (fields field).orElse (fun _ => record field)

def patchLedger (ledger : RuntimeLedger) (collection key : String)
    (fields : LedgerRecord) : RuntimeLedger :=
  fun address => if address = (collection, key) then patchRecord (ledger address) fields
    else ledger address

theorem ledger_patch_sets_supplied_field (ledger : RuntimeLedger) (collection key field value : String)
    (fields : LedgerRecord) (h : fields field = some value) :
    patchLedger ledger collection key fields (collection, key) field = some value := by
  simp [patchLedger, patchRecord, h]

theorem ledger_patch_preserves_other_fields (ledger : RuntimeLedger) (collection key field : String)
    (fields : LedgerRecord) (h : fields field = none) :
    patchLedger ledger collection key fields (collection, key) field =
      ledger (collection, key) field := by
  simp [patchLedger, patchRecord, h]

theorem ledger_patch_preserves_other_records (ledger : RuntimeLedger) (collection key : String)
    (fields : LedgerRecord) (address : String × String) (h : address ≠ (collection, key)) :
    patchLedger ledger collection key fields address = ledger address := by
  simp [patchLedger, h]

/-- Prepared turns supply independent updates to meta.state and the saved turn. -/
structure TurnProjection where
  state : Nat
  turnState : Option Nat := none
  deriving DecidableEq, Repr

structure TurnChanges where
  state : Option Nat := none
  turnState : Option Nat := none

def reduceTurnChanges (projection : TurnProjection) (changes : TurnChanges) : TurnProjection :=
  { state := changes.state.getD projection.state
    turnState := changes.turnState.orElse (fun _ => projection.turnState) }

def preparedTurnChanges (state : Nat) : TurnChanges := ⟨some state, some state⟩

theorem turn_record_preserves_meta_state (projection : TurnProjection) (turnState : Nat) :
    (reduceTurnChanges projection ⟨none, some turnState⟩).state = projection.state := rfl

theorem explicit_meta_state_wins (projection : TurnProjection) (state turnState : Nat) :
    (reduceTurnChanges projection ⟨some state, some turnState⟩).state = state := rfl

theorem prepared_turn_commits_both_states (projection : TurnProjection) (state : Nat) :
    reduceTurnChanges projection (preparedTurnChanges state) = ⟨state, some state⟩ := rfl

/-- Model-facing State puts stable JSON fields before revision and runtime.
    Fields are already encoded; this models ordering, not provider cache policy. -/
def statePrompt (schema id goal cells revision runtime : String) : List String :=
  [schema, id, goal, cells, revision, runtime]

theorem state_prompt_prefix_survives_turn_changes
    (schema id goal cells revision runtime nextRevision nextRuntime : String) :
    (statePrompt schema id goal cells revision runtime).take 4 =
      (statePrompt schema id goal cells nextRevision nextRuntime).take 4 := rfl

theorem state_prompt_retains_dynamic_fields
    (schema id goal cells revision runtime : String) :
    (statePrompt schema id goal cells revision runtime).drop 4 = [revision, runtime] := rfl

end RioAgent
