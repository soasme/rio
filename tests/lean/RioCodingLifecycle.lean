/- Session, provider, prompt, and extension decisions from rio.coding. -/
import RioCoding

namespace RioCodingLifecycle

/- session_preparation.py: staged writes become authoritative only on adoption. -/
inductive Preparation where
  | prepared | adopted | aborted
deriving DecidableEq, Repr

inductive AdoptError where
  | alreadyFinished | trustCancelled | commitFailed
deriving DecidableEq, Repr

structure PreparedSession where
  phase : Preparation := .prepared
  stagedEntries : Nat := 0
  committedEntries : Nat := 0
deriving DecidableEq, Repr

def adopt (session : PreparedSession) (trustCancelled commitSucceeds : Bool) :
    Except AdoptError PreparedSession :=
  if session.phase != .prepared then .error .alreadyFinished
  else if trustCancelled then .error .trustCancelled
  else if !commitSucceeds then .error .commitFailed
  else .ok ⟨.adopted, 0, session.committedEntries + session.stagedEntries⟩

def abort (session : PreparedSession) : PreparedSession :=
  if session.phase == .prepared then { session with phase := .aborted, stagedEntries := 0 }
  else session

theorem cancelled_adoption_commits_nothing (session : PreparedSession)
    (h : session.phase = .prepared) (commitSucceeds : Bool) :
    adopt session true commitSucceeds = .error .trustCancelled := by
  simp [adopt, h]

theorem successful_adoption_commits_staged_entries (session : PreparedSession)
    (h : session.phase = .prepared) :
    adopt session false true =
      .ok ⟨.adopted, 0, session.committedEntries + session.stagedEntries⟩ := by
  simp [adopt, h]

theorem abort_is_idempotent (session : PreparedSession) :
    abort (abort session) = abort session := by
  cases session with
  | mk phase staged committed => cases phase <;> rfl

/- session.py: a CodingSession accepts one run, even when input hooks handle it. -/
inductive RunDecision where
  | start (goal : String) | handled | alreadyRan
deriving DecidableEq, Repr

def beginRun (hasRun handled : Bool) (goal : String) : Bool × RunDecision :=
  if hasRun then (true, .alreadyRan)
  else if handled then (true, .handled)
  else (true, .start goal)

theorem handled_input_consumes_run (goal : String) :
    beginRun false true goal = (true, .handled) := by rfl

theorem second_run_rejected (handled : Bool) (goal : String) :
    beginRun true handled goal = (true, .alreadyRan) := by rfl

/- provider_config.py: explicit model and thinking overrides are strict. -/
structure Provider where
  defaultModel : String
  models : List String
deriving DecidableEq, Repr

inductive SelectionError where
  | noDefault | unknownModel
deriving DecidableEq, Repr

def selectModel (provider : Provider) (requested : Option String) :
    Except SelectionError String :=
  let model := match requested with
    | some value => if value.isEmpty then provider.defaultModel else value
    | none => provider.defaultModel
  if model.isEmpty then .error .noDefault
  else if provider.models.contains model then .ok model
  else .error .unknownModel

theorem unknown_explicit_model_rejected (provider : Provider) (model : String)
    (nonempty : model.isEmpty = false) (unknown : model ∉ provider.models) :
    selectModel provider (some model) = .error .unknownModel := by
  simp [selectModel, nonempty, unknown]

theorem declared_default_selected (provider : Provider)
    (nonempty : provider.defaultModel.isEmpty = false)
    (declared : provider.defaultModel ∈ provider.models) :
    selectModel provider none = .ok provider.defaultModel := by
  simp [selectModel, nonempty, declared]

inductive ThinkingError where
  | unavailable | unsupportedOverride
deriving DecidableEq, Repr

def startupThinking (available : List RioCoding.Thinking)
    (override remembered preferred providerDefault : Option RioCoding.Thinking) :
    Except ThinkingError (Option RioCoding.Thinking) :=
  match override with
  | some level =>
      if available.isEmpty then .error .unavailable
      else if available.contains level then .ok (some level)
      else .error .unsupportedOverride
  | none =>
      if available.isEmpty then .ok none
      else
        let fallback := (available.head?).getD .medium
        let chosen := (remembered.filter (available.contains ·)).getD
          ((preferred.filter (available.contains ·)).getD
            ((providerDefault.filter (available.contains ·)).getD fallback))
        .ok (some chosen)

theorem unsupported_thinking_override_rejected (available : List RioCoding.Thinking)
    (requested : RioCoding.Thinking) (remembered preferred fallback : Option RioCoding.Thinking)
    (nonempty : available.isEmpty = false) (unsupported : requested ∉ available) :
    startupThinking available (some requested) remembered preferred fallback =
      .error .unsupportedOverride := by
  simp [startupThinking, nonempty, unsupported]

theorem unavailable_thinking_without_override_is_none
    (remembered preferred fallback : Option RioCoding.Thinking) :
    startupThinking [] none remembered preferred fallback = .ok none := by rfl

theorem remembered_thinking_wins (available : List RioCoding.Thinking)
    (level : RioCoding.Thinking) (preferred fallback : Option RioCoding.Thinking)
    (supported : level ∈ available) :
    startupThinking available none (some level) preferred fallback = .ok (some level) := by
  cases available with
  | nil => simp at supported
  | cons first rest =>
      have h : (decide (level = first) || decide (level ∈ rest)) = true := by
        simpa [List.contains_cons] using supported
      simp [startupThinking, Option.filter, h]

/- session.py: expansion precedence for explicit skills and bare slash names. -/
inductive PromptSource where
  | unchanged | explicitSkill | builtinCommand | template | bareSkill
deriving DecidableEq, Repr

def promptSource (explicitSkill builtin template bareSkill : Bool) : PromptSource :=
  if explicitSkill then .explicitSkill
  else if builtin then .builtinCommand
  else if template then .template
  else if bareSkill then .bareSkill
  else .unchanged

theorem explicit_skill_takes_precedence (builtin template bareSkill : Bool) :
    promptSource true builtin template bareSkill = .explicitSkill := by rfl

theorem builtin_shadows_template_and_skill (template bareSkill : Bool) :
    promptSource false true template bareSkill = .builtinCommand := by rfl

/- extensions/runtime.py: transforms chain; handled stops; invalid hooks are isolated. -/
inductive InputHook where
  | ignore | transform (text : String) | handled | failed | invalid
deriving DecidableEq, Repr

structure InputOutcome where
  text : String
  handled : Bool
deriving DecidableEq, Repr

def applyHook (current : InputOutcome) (hook : InputHook) : InputOutcome :=
  if current.handled then current
  else match hook with
    | .transform text => { current with text }
    | .handled => { current with handled := true }
    | _ => current

def runHooks (text : String) (hooks : List InputHook) : InputOutcome :=
  hooks.foldl applyHook { text, handled := false }

theorem failed_hook_preserves_input (text : String) :
    runHooks text [.failed] = { text, handled := false } := by rfl

theorem transforms_chain (original first second : String) :
    runHooks original [.transform first, .transform second] =
      { text := second, handled := false } := by rfl

theorem handled_stops_later_transforms (original first ignored : String) :
    runHooks original [.transform first, .handled, .transform ignored] =
      { text := first, handled := true } := by rfl

end RioCodingLifecycle
