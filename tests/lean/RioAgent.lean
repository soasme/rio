/- JSON state and patch loop decisions in rio.agent. Patch application is an input. -/
namespace RioAgent

structure Step where
  state : Nat
  reply : Option String
  deriving DecidableEq, Repr

inductive Attempt where
  | invalidPatch
  | malformedCall
  | accepted (state : Nat) (reply : Option String)
  deriving DecidableEq, Repr

inductive Result where
  | retry
  | committed (step : Step)
  deriving DecidableEq, Repr

def runAttempt : Attempt → Result
  | .invalidPatch => .retry
  | .malformedCall => .retry
  | .accepted state reply => .committed ⟨state, reply⟩

theorem invalid_patch_retries : runAttempt .invalidPatch = .retry := rfl
theorem malformed_call_retries : runAttempt .malformedCall = .retry := rfl
theorem accepted_patch_updates_state (state : Nat) (reply : Option String) :
    runAttempt (.accepted state reply) = .committed ⟨state, reply⟩ := rfl

def shouldStop (reply : Option String) : Bool :=
  match reply with
  | some text => !text.isEmpty
  | none => false

theorem reply_ends_run (text : String) (h : text.isEmpty = false) :
    shouldStop (some text) = true := by simp [shouldStop, h]
theorem no_reply_continues : shouldStop none = false := rfl

end RioAgent
