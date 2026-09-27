/- End-to-end trial verdict in evals/run.py. -/
namespace RioEvals

def passed (agentFinished graderPassed : Bool) : Bool :=
  agentFinished && graderPassed

theorem failed_agent_cannot_pass (graderPassed : Bool) :
    passed false graderPassed = false := by rfl

theorem failed_grader_cannot_pass (agentFinished : Bool) :
    passed agentFinished false = false := by
  cases agentFinished <;> rfl

theorem successful_trial_passes : passed true true = true := by rfl

end RioEvals
