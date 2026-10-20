namespace RioEvals

inductive TestOutcome where
  | passed | failed | skipped
  deriving DecidableEq, BEq

structure Grade where
  exitZero : Bool
  timedOut : Bool
  required : List String
  collected : List String
  finished : List (String × TestOutcome)

def accepted (g : Grade) : Bool :=
  g.exitZero && !g.timedOut && !g.required.isEmpty &&
  g.collected.length == g.required.length &&
  g.required.all g.collected.contains &&
  g.finished.length == g.required.length &&
  g.required.all (fun name => g.finished.contains (name, .passed))

theorem timeout_never_passes (g : Grade) :
    accepted { g with timedOut := true } = false := by
  simp [accepted]

theorem zero_exit_without_tests_never_passes (g : Grade) :
    accepted { g with required := [] } = false := by
  simp [accepted]

theorem skipped_is_not_success :
    accepted ⟨true, false, ["test"], ["test"], [("test", .skipped)]⟩ = false := by
  decide

theorem missing_required_test_is_not_success :
    accepted ⟨true, false, ["a", "b"], ["a"], [("a", .passed)]⟩ = false := by
  decide

structure Manifest where
  complete : Bool
  unchanged : Bool
  calibrated : Bool
  allScheduledPresent : Bool
  compatible : Bool

def comparable (m : Manifest) : Bool :=
  m.complete && m.unchanged && m.calibrated && m.allScheduledPresent && m.compatible

theorem changed_inputs_cannot_compare (m : Manifest) :
    comparable { m with unchanged := false } = false := by
  simp [comparable]

theorem dropped_trials_cannot_compare (m : Manifest) :
    comparable { m with allScheduledPresent := false } = false := by
  simp [comparable]

/-- The original and final file trees determine the artifact, independently of Git's mutable index. -/
def changed (original final : List (String × String)) (_index _head : String) :
    List (String × String) :=
  final.filter (fun file => !original.contains file)

theorem staging_and_committing_preserve_artifact (original final : List (String × String))
    (index₁ index₂ head₁ head₂ : String) :
    changed original final index₁ head₁ = changed original final index₂ head₂ := rfl

def tokenCost (successful : Nat) (costs : List (Option Nat)) : Option Nat :=
  if successful == 0 || costs.any Option.isNone then none
  else some (costs.foldl (fun total cost => total + cost.getD 0) 0 / successful)

theorem unknown_cost_is_not_free :
    tokenCost 1 [some 10, none] = none := by decide

theorem failed_attempts_still_cost :
    tokenCost 1 [some 10, some 30] = some 40 := by decide

end RioEvals
