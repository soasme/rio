/- A formal control-flow model of `rio.agent.loop.run_context_loop`.

The model abstracts provider I/O, the context file's text format, and token
estimation. A context is a list of turns; its size is the total text length.
It retains the loop's decisions: edit adoption under the limit, the overflow
guard, retries for replies without a usable action, provider failures, action
failure recovery, termination, cancellation, and the step limit.

`ReplyError` represents every rejected reply path in the Python runtime: no
tool call, unparseable arguments, and an unknown tool.
-/
namespace RioAgent

structure Turn where
  role : String
  text : String
deriving DecidableEq, Repr

abbrev Context := List Turn

def size (context : Context) : Nat := (context.map (·.text.length)).sum

/-! ## Adopting an edit to the context file -/

inductive EditOutcome where
  | unchanged
  | accepted
  | rejected
deriving DecidableEq, Repr

/-- `edited` is the parsed file after the action, or `none` when it did not change. -/
def adoptEdit (limit : Nat) (context : Context) : Option Context → Context × EditOutcome
  | none => (context, .unchanged)
  | some candidate =>
      if size candidate <= limit then (candidate, .accepted) else (context, .rejected)

theorem unchanged_file_keeps_context (limit : Nat) (context : Context) :
    adoptEdit limit context none = (context, .unchanged) := rfl

theorem edit_within_limit_is_adopted (limit : Nat) (context candidate : Context)
    (h : size candidate <= limit) :
    adoptEdit limit context (some candidate) = (candidate, .accepted) := by
  simp [adoptEdit, h]

theorem edit_over_limit_keeps_context (limit : Nat) (context candidate : Context)
    (h : limit < size candidate) :
    adoptEdit limit context (some candidate) = (context, .rejected) := by
  simp [adoptEdit, Nat.not_le.mpr h]

/-- An adopted context never exceeds the limit unless it was already over it. -/
theorem adopted_context_fits (limit : Nat) (context : Context) (edited : Option Context)
    (h : size context <= limit) :
    size (adoptEdit limit context edited).1 <= limit := by
  cases edited with
  | none => simpa [adoptEdit] using h
  | some candidate =>
      by_cases hc : size candidate <= limit
      · simp [adoptEdit, hc]
      · simp [adoptEdit, hc, h]

/-! ## The overflow guard -/

def withheldNote : String := "[withheld by the runtime]"

/-- Withhold the bodies of the oldest `count` non-user turns. -/
def withhold : Nat → Context → Context
  | 0, context => context
  | _, [] => []
  | count + 1, turn :: rest =>
      if turn.role = "user" then turn :: withhold (count + 1) rest
      else { turn with text := withheldNote } :: withhold count rest

/-- The guard never touches the newest turn: the observation the model has not seen. -/
def guard (count : Nat) (context : Context) : Context :=
  match context.getLast? with
  | none => []
  | some newest => withhold count context.dropLast ++ [newest]

theorem withhold_keeps_roles (count : Nat) (context : Context) :
    (withhold count context).map (·.role) = context.map (·.role) := by
  induction context generalizing count with
  | nil => cases count <;> rfl
  | cons turn rest ih =>
      cases count with
      | zero => rfl
      | succ count => by_cases h : turn.role = "user" <;> simp [withhold, h, ih]

theorem withhold_keeps_user_turns (count : Nat) (context : Context) :
    (withhold count context).filter (·.role = "user") = context.filter (·.role = "user") := by
  induction context generalizing count with
  | nil => cases count <;> rfl
  | cons turn rest ih =>
      cases count with
      | zero => rfl
      | succ count => by_cases h : turn.role = "user" <;> simp [withhold, h, ih]

theorem guard_keeps_the_newest_turn (count : Nat) (history : Context) (newest : Turn) :
    guard count (history ++ [newest]) = withhold count history ++ [newest] := by
  simp [guard]

/-! ## One step -/

inductive ReplyError where
  | noToolCall
  | malformedArguments
  | unknownTool
deriving DecidableEq, Repr

inductive ActionOutcome where
  | succeeded (terminated : Bool) (edited : Option Context)
  | failed (edited : Option Context)

inductive Response where
  | providerError
  | rejected (error : ReplyError)
  | call (actionName : String) (outcome : ActionOutcome)

def reply (actionName : String) : Turn := { role := "assistant", text := actionName }
def observation : Turn := { role := "tool", text := "observation" }

structure StepResult where
  context : Context
  actionName : String
  actionFailed : Bool
  edit : EditOutcome
  terminated : Bool

/-- The context after a step: the adopted edit, then the step's reply and observation. -/
def commit (limit : Nat) (context : Context) (actionName : String) (failed terminated : Bool)
    (edited : Option Context) : StepResult :=
  let (adopted, edit) := adoptEdit limit context edited
  { context := adopted ++ [reply actionName, observation], actionName := actionName,
    actionFailed := failed, edit := edit, terminated := terminated }

inductive AttemptResult where
  | retry (error : ReplyError)
  | providerError
  | committed (result : StepResult)

def runAttempt (limit : Nat) (context : Context) : Response → AttemptResult
  | .providerError => .providerError
  | .rejected error => .retry error
  | .call name (.succeeded terminated edited) =>
      .committed (commit limit context name false terminated edited)
  | .call name (.failed edited) => .committed (commit limit context name true false edited)

theorem unedited_step_appends_reply_and_observation (limit : Nat) (context : Context)
    (name : String) (terminated : Bool) :
    runAttempt limit context (.call name (.succeeded terminated none)) =
      .committed
        { context := context ++ [reply name, observation], actionName := name,
          actionFailed := false, edit := .unchanged, terminated := terminated } := rfl

theorem accepted_edit_replaces_the_context (limit : Nat) (context candidate : Context)
    (name : String) (terminated : Bool) (h : size candidate <= limit) :
    runAttempt limit context (.call name (.succeeded terminated (some candidate))) =
      .committed
        { context := candidate ++ [reply name, observation], actionName := name,
          actionFailed := false, edit := .accepted, terminated := terminated } := by
  simp [runAttempt, commit, adoptEdit, h]

theorem failed_actions_are_observations_not_terminations (limit : Nat) (context : Context)
    (name : String) :
    runAttempt limit context (.call name (.failed none)) =
      .committed
        { context := context ++ [reply name, observation], actionName := name,
          actionFailed := true, edit := .unchanged, terminated := false } := rfl

theorem rejected_replies_do_not_execute (limit : Nat) (context : Context) (error : ReplyError) :
    runAttempt limit context (.rejected error) = .retry error := rfl

inductive StepStop where
  | committed (result : StepResult)
  | providerError
  | retriesExhausted
  | inputExhausted

/-- Try at most `maxRetries + 1` responses for a step. -/
def runStep (limit : Nat) (context : Context) (maxRetries : Nat) : List Response → StepStop
  | [] => .inputExhausted
  | response :: rest =>
      match runAttempt limit context response with
      | .committed result => .committed result
      | .providerError => .providerError
      | .retry _ =>
          match maxRetries with
          | 0 => .retriesExhausted
          | retries + 1 => runStep limit context retries rest

theorem provider_failures_are_not_retried (limit retries : Nat) (context : Context)
    (responses : List Response) :
    runStep limit context retries (.providerError :: responses) = .providerError := by
  unfold runStep
  rfl

theorem retry_limit_is_max_retries_plus_one (limit : Nat) (context : Context)
    (error : ReplyError) (responses : List Response) :
    runStep limit context 0 (.rejected error :: responses) = .retriesExhausted := rfl

theorem a_retry_then_a_call_commits (limit : Nat) (context : Context) (error : ReplyError)
    (name : String) :
    runStep limit context 1 [.rejected error, .call name (.succeeded false none)] =
      .committed (commit limit context name false false none) := rfl

/-! ## Events -/

inductive Event where
  | stepStart (context : Context)
  | validationError (error : ReplyError)
  | actionStart (name : String)
  | actionEnd (name : String) (isError : Bool)
  | contextEdit (accepted : Bool)
  | stepEnd (context : Context) (terminated : Bool)

/-- Events after a call is accepted: the action, then any edit, then the step end. -/
def committedTrace (result : StepResult) : List Event :=
  [.actionStart result.actionName, .actionEnd result.actionName result.actionFailed] ++
    (match result.edit with
      | .unchanged => []
      | .accepted => [.contextEdit true]
      | .rejected => [.contextEdit false]) ++
    [.stepEnd result.context result.terminated]

theorem committed_trace_runs_the_action_first (result : StepResult) :
    (committedTrace result).take 2 =
      [.actionStart result.actionName, .actionEnd result.actionName result.actionFailed] := by
  cases h : result.edit <;> simp [committedTrace, h]

theorem committed_trace_ends_with_the_new_context (result : StepResult) :
    ∃ before, committedTrace result = before ++ [.stepEnd result.context result.terminated] := by
  cases h : result.edit
  · exact ⟨[.actionStart result.actionName, .actionEnd result.actionName result.actionFailed],
      by simp [committedTrace, h]⟩
  · exact ⟨[.actionStart result.actionName, .actionEnd result.actionName result.actionFailed,
      .contextEdit true], by simp [committedTrace, h]⟩
  · exact ⟨[.actionStart result.actionName, .actionEnd result.actionName result.actionFailed,
      .contextEdit false], by simp [committedTrace, h]⟩

/-- A step emits `StepStart` once, before any validation or action event. -/
def stepTrace (limit : Nat) (context : Context) (maxRetries : Nat) :
    List Response → List Event
  | responses => .stepStart context :: attemptsTrace maxRetries responses
where
  attemptsTrace : Nat → List Response → List Event
    | _, [] => []
    | retries, response :: rest =>
        match runAttempt limit context response with
        | .committed result => committedTrace result
        | .providerError => []
        | .retry error =>
            .validationError error ::
              match retries with
              | 0 => []
              | retries + 1 => attemptsTrace retries rest

theorem step_trace_starts_before_all_attempt_events (limit retries : Nat) (context : Context)
    (responses : List Response) :
    (stepTrace limit context retries responses).take 1 = [.stepStart context] := rfl

theorem rejected_reply_emits_only_validation (limit : Nat) (context : Context)
    (error : ReplyError) :
    stepTrace limit context 0 [.rejected error] = [.stepStart context, .validationError error] :=
  rfl

/-! ## The outer loop -/

inductive RunStop where
  | terminated
  | cancelled
  | stepLimit
  | providerError
  | retriesExhausted
  | inputExhausted

structure RunResult where
  context : Context
  steps : Nat
  stop : RunStop

/-- Each cancellation flag is checked before its step. -/
def runLoop (limit maxRetries : Nat) :
    Nat → Context → List Bool → List (List Response) → RunResult
  | 0, context, _, _ => { context, steps := 0, stop := .stepLimit }
  | _ + 1, context, true :: _, _ => { context, steps := 0, stop := .cancelled }
  | _ + 1, context, _, [] => { context, steps := 0, stop := .inputExhausted }
  | remaining + 1, context, flags, responses :: rest =>
      match runStep limit context maxRetries responses with
      | .committed result =>
          if result.terminated then
            { context := result.context, steps := 1, stop := .terminated }
          else
            let next := runLoop limit maxRetries remaining result.context flags.tail rest
            { next with steps := next.steps + 1 }
      | .providerError => { context, steps := 0, stop := .providerError }
      | .retriesExhausted => { context, steps := 0, stop := .retriesExhausted }
      | .inputExhausted => { context, steps := 0, stop := .inputExhausted }

theorem cancellation_preserves_context (limit retries : Nat) (context : Context)
    (responses : List (List Response)) :
    runLoop limit retries 1 context [true] responses =
      { context, steps := 0, stop := .cancelled } := by
  simp [runLoop]

theorem step_limit_prevents_model_calls (limit retries : Nat) (context : Context)
    (cancelled : List Bool) (responses : List (List Response)) :
    runLoop limit retries 0 context cancelled responses =
      { context, steps := 0, stop := .stepLimit } := rfl

theorem terminating_action_ends_the_run (limit retries : Nat) (context : Context)
    (name : String) :
    runLoop limit retries 1 context [] [[.call name (.succeeded true none)]] =
      { context := context ++ [reply name, observation], steps := 1, stop := .terminated } := by
  cases retries <;> rfl

end RioAgent
