/- A formal control-flow model of `rio.agent.loop.run_skill_loop`.

The model abstracts provider I/O and text event payloads. It represents the JSON
Merge Patch state transition used by the runtime and retains the loop's
decisions: validation rollback and retry, provider failures, state commit
before action execution, action failure recovery, follow-up action deltas,
termination, cancellation, and the step limit.

`ValidationError` represents every rejected proposal path in the Python
runtime. `accepted` represents a parsed proposal with a declared action and
object arguments; the remaining state-budget check is modeled by `valid`.
-/
namespace RioAgent

inductive Json where
  | null
  | boolean (value : Bool)
  | number (value : Nat)
  | string (value : String)
  | array (values : List Json)
  | object (fields : List (String × Json))

abbrev Object := List (String × Json)
abbrev State := Object
abbrev Delta := Object

def lookup (key : String) : Object → Option Json
  | [] => none
  | (current, value) :: fields => if current = key then some value else lookup key fields

def removeKey (key : String) : Object → Object
  | [] => []
  | (current, value) :: fields =>
      if current = key then removeKey key fields else (current, value) :: removeKey key fields

def put (key : String) (value : Json) (fields : Object) : Object :=
  (key, value) :: removeKey key fields

/-- RFC 7396 JSON Merge Patch, restricted at the top level to JSON objects. -/
def mergePatch (target patch : Json) : Json :=
  match patch with
  | .object fields => .object (mergeFields (match target with | .object object => object | _ => []) fields)
  | value => value
where
  mergeFields (target : Object) : Object → Object
    | [] => target
    | (key, .null) :: fields => mergeFields (removeKey key target) fields
    | (key, value) :: fields =>
        let previous := (lookup key target).getD .null
        mergeFields (put key (mergePatch previous value) target) fields

def applyDelta (state : State) (delta : Delta) : State :=
  match mergePatch (.object state) (.object delta) with
  | .object fields => fields
  | _ => []

/-- The runtime's prompt history: a materialized baseline followed by accepted patches.
Observations are intentionally omitted because they do not alter execution state. -/
structure StateHistory where
  baseline : State
  patches : List Delta

def buildState : StateHistory → State
  | ⟨baseline, patches⟩ => patches.foldl applyDelta baseline

/-- Compaction keeps exactly the materialized state and drops only replayable history. -/
def rebuildHistory (history : StateHistory) : StateHistory :=
  ⟨buildState history, []⟩

theorem rebuild_history_preserves_state (history : StateHistory) :
    buildState (rebuildHistory history) = buildState history := by
  simp [rebuildHistory, buildState]

theorem lookup_put_same (key : String) (value : Json) (fields : Object) :
    lookup key (put key value fields) = some value := by
  simp [lookup, put]

theorem lookup_remove_none (key : String) (fields : Object) :
    lookup key (removeKey key fields) = none := by
  induction fields with
  | nil => rfl
  | cons field fields ih =>
      rcases field with ⟨current, value⟩
      by_cases h : current = key <;> simp [lookup, removeKey, h, ih]

theorem null_patch_deletes_field (state : State) (key : String) :
    lookup key (applyDelta state [(key, .null)]) = none := by
  simp [applyDelta, mergePatch, mergePatch.mergeFields, lookup_remove_none]

theorem number_patch_replaces_field (state : State) (key : String) (value : Nat) :
    lookup key (applyDelta state [(key, .number value)]) = some (.number value) := by
  simp [applyDelta, mergePatch, mergePatch.mergeFields, lookup, put]

theorem object_patch_recurses (state : State) (key : String) (previous : Json) (patch : Object)
    (h : lookup key state = some previous) :
    lookup key (applyDelta state [(key, .object patch)]) =
      some (mergePatch previous (.object patch)) := by
  simp [applyDelta, mergePatch, mergePatch.mergeFields, h, lookup, put]

/-- Structural size proxy for the separately modeled state-budget gate. -/
def serializedSize (state : State) : Nat := state.length

def valid (budget : Nat) (state : State) (delta : Delta) : Bool :=
  serializedSize (applyDelta state delta) <= budget

inductive ValidationError where
  | missingStep
  | malformedArguments
  | malformedStateDelta
  | undeclaredField
  | malformedAction
  | unknownAction
  | malformedActionArguments
  | stateOverBudget
deriving DecidableEq, Repr

inductive ActionOutcome where
  | succeeded (stateDelta : Delta) (terminated : Bool)
  | failed

inductive Response where
  | providerError
  | rejected (error : ValidationError)
  | accepted (actionName : String) (stateDelta : Delta) (outcome : ActionOutcome)

structure StepResult where
  committedState : State
  state : State
  actionName : String
  actionDelta : Delta
  actionExecuted : Bool
  actionFailed : Bool
  terminated : Bool

inductive AttemptResult where
  | retry
  | providerError
  | committed (result : StepResult)

/-- One model response. Invalid proposals never execute an action. -/
def runAttempt (budget : Nat) (state : State) : Response → AttemptResult
  | .providerError => .providerError
  | .rejected _ => .retry
  | .accepted actionName proposed outcome =>
      if valid budget state proposed then
        let committed := applyDelta state proposed
        match outcome with
        | .succeeded actionDelta terminated =>
            .committed {
              committedState := committed
              state := applyDelta committed actionDelta
              actionName := actionName
              actionDelta := actionDelta
              actionExecuted := true
              actionFailed := false
              terminated := terminated
            }
        | .failed =>
            .committed {
              committedState := committed
              state := committed
              actionName := actionName
              actionDelta := []
              actionExecuted := true
              actionFailed := true
              terminated := false
            }
      else
        .retry

inductive StepStop where
  | committed (result : StepResult)
  | providerError
  | retriesExhausted
  | inputExhausted

/-- Try at most `maxRetries + 1` responses for a step. -/
def runStep (budget : Nat) (state : State) (maxRetries : Nat) : List Response → StepStop
  | [] => .inputExhausted
  | response :: rest =>
      match runAttempt budget state response with
      | .committed result => .committed result
      | .providerError => .providerError
      | .retry =>
          match maxRetries with
          | 0 => .retriesExhausted
          | retries + 1 => runStep budget state retries rest

inductive RunStop where
  | terminated
  | cancelled
  | stepLimit
  | providerError
  | retriesExhausted
  | inputExhausted

structure RunResult where
  state : State
  steps : Nat
  stop : RunStop

inductive Event where
  | stepStart (state : State)
  | validationError (error : ValidationError)
  | stateUpdate (state : State)
  | actionStart (name : String)
  | actionEnd (name : String) (isError : Bool)
  | stepEnd (state : State) (terminated : Bool)

/-- The per-step event sequence after a proposal has been committed. -/
def committedTrace (result : StepResult) : List Event :=
  [.stateUpdate result.committedState, .actionStart result.actionName,
    .actionEnd result.actionName result.actionFailed] ++
    (if result.actionDelta.isEmpty then [] else [.stateUpdate result.state]) ++
    [.stepEnd result.state result.terminated]

theorem committed_trace_begins_with_commit_then_action (result : StepResult) :
    (committedTrace result).take 2 =
      [.stateUpdate result.committedState, .actionStart result.actionName] := by
  simp [committedTrace]

theorem committed_trace_ends_with_final_state (result : StepResult) :
    ∃ before, committedTrace result = before ++ [.stepEnd result.state result.terminated] := by
  refine ⟨[.stateUpdate result.committedState, .actionStart result.actionName,
    .actionEnd result.actionName result.actionFailed] ++
      (if result.actionDelta.isEmpty then [] else [.stateUpdate result.state]), ?_⟩
  simp [committedTrace]

theorem nonempty_action_delta_emits_final_state (result : StepResult)
    (h : result.actionDelta.isEmpty = false) :
    committedTrace result = [.stateUpdate result.committedState, .actionStart result.actionName,
      .actionEnd result.actionName result.actionFailed, .stateUpdate result.state,
      .stepEnd result.state result.terminated] := by
  simp [committedTrace, h]

/-- Events produced while retrying one model response. Text payloads are opaque. -/
def attemptsTrace (budget : Nat) (state : State) (maxRetries : Nat) : List Response → List Event
  | [] => []
  | .providerError :: _ => []
  | .rejected error :: responses =>
      .validationError error ::
        match maxRetries with
        | 0 => []
        | retries + 1 => attemptsTrace budget state retries responses
  | response :: responses =>
      match runAttempt budget state response with
      | .committed result => committedTrace result
      | .providerError => []
      | .retry =>
          .validationError .stateOverBudget ::
            match maxRetries with
            | 0 => []
            | retries + 1 => attemptsTrace budget state retries responses

/-- A step emits `StepStart` once, before any validation or action event. -/
def stepTrace (budget : Nat) (state : State) (maxRetries : Nat) (responses : List Response) : List Event :=
  .stepStart state :: attemptsTrace budget state maxRetries responses

theorem rejected_attempt_emits_only_validation (budget : Nat) (state : State)
    (error : ValidationError) :
    attemptsTrace budget state 0 [.rejected error] = [.validationError error] := by
  rfl

theorem step_trace_starts_before_all_attempt_events (budget retries : Nat) (state : State)
    (responses : List Response) :
    (stepTrace budget state retries responses).take 1 = [.stepStart state] := by
  rfl

/-- Model the outer loop. Each cancellation flag is checked before its step. -/
def runLoop (budget maxRetries : Nat) :
    Nat → State → List Bool → List (List Response) → RunResult
  | 0, state, _, _ => { state, steps := 0, stop := .stepLimit }
  | _ + 1, state, true :: _, _ =>
      { state, steps := 0, stop := .cancelled }
  | _ + 1, state, [], [] =>
      { state, steps := 0, stop := .inputExhausted }
  | remaining + 1, state, [], responses :: rest =>
      match runStep budget state maxRetries responses with
      | .committed result =>
          if result.terminated then
            { state := result.state, steps := 1, stop := .terminated }
          else
            let next := runLoop budget maxRetries remaining result.state [] rest
            { state := next.state, steps := next.steps + 1, stop := next.stop }
      | .providerError => { state, steps := 0, stop := .providerError }
      | .retriesExhausted => { state, steps := 0, stop := .retriesExhausted }
      | .inputExhausted => { state, steps := 0, stop := .inputExhausted }
  | remaining + 1, state, false :: cancelled, responses :: rest =>
      match runStep budget state maxRetries responses with
      | .committed result =>
          if result.terminated then
            { state := result.state, steps := 1, stop := .terminated }
          else
            let next := runLoop budget maxRetries remaining result.state cancelled rest
            { state := next.state, steps := next.steps + 1, stop := next.stop }
      | .providerError => { state, steps := 0, stop := .providerError }
      | .retriesExhausted => { state, steps := 0, stop := .retriesExhausted }
      | .inputExhausted => { state, steps := 0, stop := .inputExhausted }
  | _ + 1, state, false :: _, [] =>
      { state, steps := 0, stop := .inputExhausted }

theorem rejected_proposals_do_not_execute (budget : Nat) (state : State) (error : ValidationError) :
    runAttempt budget state (.rejected error) = .retry := by
  rfl

theorem invalid_proposals_rollback (budget : Nat) (state : State) (proposed : Delta)
    (actionName : String) (outcome : ActionOutcome)
    (h : valid budget state proposed = false) :
    runAttempt budget state (.accepted actionName proposed outcome) = .retry := by
  simp [runAttempt, h]

theorem provider_failures_are_not_retried (budget retries : Nat) (state : State)
    (responses : List Response) :
    runStep budget state retries (.providerError :: responses) = .providerError := by
  unfold runStep
  rfl

theorem retry_limit_is_max_retries_plus_one (budget : Nat) (state : State)
    (error : ValidationError) :
    runStep budget state 0 [.rejected error] = .retriesExhausted := by
  rfl

theorem failed_actions_keep_the_committed_state (budget : Nat) (state : State) (actionName : String)
    (proposed : Delta)
    (h : valid budget state proposed = true) :
    runAttempt budget state (.accepted actionName proposed .failed) =
      AttemptResult.committed ⟨applyDelta state proposed, applyDelta state proposed, actionName, [],
        true, true, false⟩ := by
  simp [runAttempt, h]

theorem successful_actions_apply_their_follow_up_delta (budget : Nat) (state : State)
    (actionName : String) (proposed actionDelta : Delta) (terminated : Bool)
    (h : valid budget state proposed = true) :
    runAttempt budget state (.accepted actionName proposed (.succeeded actionDelta terminated)) =
      AttemptResult.committed ⟨applyDelta state proposed,
        applyDelta (applyDelta state proposed) actionDelta, actionName, actionDelta, true, false, terminated⟩ := by
  simp [runAttempt, h]

theorem cancellation_preserves_state (budget retries : Nat) (state : State)
    (responses : List (List Response)) :
    runLoop budget retries 1 state [true] responses =
      { state, steps := 0, stop := .cancelled } := by
  simp [runLoop]

theorem step_limit_prevents_model_calls (budget retries : Nat) (state : State)
    (cancelled : List Bool) (responses : List (List Response)) :
    runLoop budget retries 0 state cancelled responses =
      { state, steps := 0, stop := .stepLimit } := by
  rfl

theorem terminating_action_ends_the_run (budget retries : Nat) (state : State) (actionName : String)
    (proposed actionDelta : Delta)
    (h : valid budget state proposed = true) :
    runLoop budget retries 1 state [] [[.accepted actionName proposed (.succeeded actionDelta true)]] =
      { state := applyDelta (applyDelta state proposed) actionDelta, steps := 1,
        stop := .terminated } := by
  have committed :
      runStep budget state retries [.accepted actionName proposed (.succeeded actionDelta true)] =
        .committed ⟨applyDelta state proposed, applyDelta (applyDelta state proposed) actionDelta,
          actionName, actionDelta, true, false, true⟩ := by
    unfold runStep
    simp [runAttempt, h]
  unfold runLoop
  rw [committed]
  rfl

end RioAgent
