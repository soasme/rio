/- Independent behavior model of final-only provider collection and retry isolation. -/
namespace RioAI

inductive Event where
  | delta (text : String)
  | retry
  | done
  | error
  deriving DecidableEq, Repr

inductive Outcome where
  | done (fragments : List String)
  | error (fragments : List String)
  deriving DecidableEq, Repr

-- Each delta is stored once. Joining and publishing happen only at completion.
def collect (fragments : List String) : List Event → Outcome
  | [] => .error fragments.reverse
  | .delta text :: rest => collect (text :: fragments) rest
  | .retry :: rest => collect [] rest
  | .done :: _ => .done fragments.reverse
  | .error :: _ => .error fragments.reverse

theorem incomplete_stream_is_error (fragments : List String) :
    collect fragments [] = .error fragments.reverse := by rfl

theorem retry_discards_previous_attempt (fragments : List String) (rest : List Event) :
    collect fragments (.retry :: rest) = collect [] rest := by rfl

theorem first_terminal_wins (fragments : List String) (rest : List Event) :
    collect fragments (.done :: rest) = .done fragments.reverse := by rfl

theorem fragments_keep_their_order (first second : String) :
    collect [] [.delta first, .delta second, .done] = .done [first, second] := by rfl

inductive StopReason where
  | length
  | toolUse
  | stop
  deriving DecidableEq, Repr

def finishReason (truncated hasTools : Bool) : StopReason :=
  if truncated then .length else if hasTools then .toolUse else .stop

theorem truncation_precedes_tool_use (hasTools : Bool) :
    finishReason true hasTools = .length := by rfl

def mayRetry (emittedContent : Bool) (attempt budget : Nat) : Bool :=
  !emittedContent && attempt < budget

theorem published_content_prevents_retry (attempt budget : Nat) :
    mayRetry true attempt budget = false := by simp [mayRetry]

theorem exhausted_budget_prevents_retry (emittedContent : Bool) (budget : Nat) :
    mayRetry emittedContent budget budget = false := by simp [mayRetry]

-- Adapters must translate clean EOF without completion metadata into an error.
def adapterEnd (hasFinishReason : Bool) : Event :=
  if hasFinishReason then .done else .error

theorem missing_finish_reason_cannot_complete (fragments : List String) :
    collect fragments [adapterEnd false] = .error fragments.reverse := by rfl

theorem finished_adapter_preserves_output (fragments : List String) :
    collect fragments [adapterEnd true] = .done fragments.reverse := by rfl

end RioAI
