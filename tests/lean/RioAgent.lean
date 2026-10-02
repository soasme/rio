/- A formal control-flow model of `rio.agent.loop.run_notebook_loop`.

The context is a notebook: a list of cells. The model abstracts provider I/O,
JSON Patch application, nbformat validation, and kernel execution. A patch is
given as its result (`none` when it fails to apply or leaves an invalid
notebook), and `run` gives the outputs of a cell that runs. Notebook size is
total text length rather than a token estimate.

It retains the loop's decisions: which cells run, which outputs are kept, the
size gate on patches, retries for unusable replies, provider failures, event
order, the reply that ends a run, cancellation, and the step limit.
-/
namespace RioAgent

structure Cell where
  id : Nat
  code : Bool
  source : String
  outputs : List String
deriving DecidableEq, Repr

abbrev Notebook := List Cell

def cellSize (cell : Cell) : Nat := cell.source.length + (cell.outputs.map String.length).sum

def size (notebook : Notebook) : Nat := (notebook.map cellSize).sum

/-! ## Which cells run -/

def sourceOf (before : Notebook) (id : Nat) : Option String :=
  (before.find? (·.id == id)).map (·.source)

/-- A code cell runs when it is new or its source changed. -/
def isChanged (before : Notebook) (cell : Cell) : Bool :=
  cell.code && sourceOf before cell.id != some cell.source

def changed (before after : Notebook) : List Cell := after.filter (isChanged before)

theorem markdown_cells_never_run (before after : Notebook) (cell : Cell)
    (h : cell ∈ changed before after) : cell.code = true := by
  simp [changed, isChanged, List.mem_filter] at h
  exact h.2.1

theorem same_source_does_not_run (before : Notebook) (cell : Cell)
    (h : sourceOf before cell.id = some cell.source) : isChanged before cell = false := by
  simp [isChanged, h]

theorem edited_outputs_do_not_run (before : Notebook) (cell : Cell) (outputs : List String)
    (h : sourceOf before cell.id = some cell.source) :
    isChanged before { cell with outputs := outputs } = false := by
  simp [isChanged, h]

/-! ## Merging outputs -/

/-- Changed cells take the outputs of their run; every other cell keeps its own. -/
def mergeCell (before : Notebook) (run : Cell → List String) (cell : Cell) : Cell :=
  if isChanged before cell then { cell with outputs := run cell } else cell

def merge (before : Notebook) (run : Cell → List String) (after : Notebook) : Notebook :=
  after.map (mergeCell before run)

theorem unchanged_cells_keep_their_outputs (before : Notebook) (run : Cell → List String)
    (cell : Cell) (h : isChanged before cell = false) : mergeCell before run cell = cell := by
  simp [mergeCell, h]

theorem changed_cells_get_new_outputs (before : Notebook) (run : Cell → List String)
    (cell : Cell) (h : isChanged before cell = true) :
    (mergeCell before run cell).outputs = run cell := by
  simp [mergeCell, h]

theorem merge_keeps_every_cell (before : Notebook) (run : Cell → List String)
    (after : Notebook) : ((merge before run after).map (·.id)) = after.map (·.id) := by
  simp [merge, mergeCell, Function.comp_def]
  intro cell _
  split <;> rfl

/-! ## The size gate -/

/-- A patch is accepted when the result fits, or when it shrinks an oversized notebook. -/
def accepts (limit : Nat) (current candidate : Notebook) : Bool :=
  size candidate <= limit || size candidate < size current

theorem accepted_patch_fits_or_shrinks (limit : Nat) (current candidate : Notebook)
    (h : accepts limit current candidate = true) :
    size candidate <= limit ∨ size candidate < size current := by
  simpa [accepts] using h

/-! ## One step -/

inductive ReplyError where
  | noStepCall
  | malformedArguments
  | invalidPatch
  | overLimit
deriving DecidableEq, Repr

inductive Response where
  | providerError
  | noStepCall
  | malformedArguments
  /-- `patched` is `none` when the patch fails or the notebook is invalid. -/
  | step (patched : Option Notebook) (reply : Option String)

def replyCell (text : String) : Cell := { id := 0, code := false, source := text, outputs := [] }

structure StepResult where
  notebook : Notebook
  ran : List Cell
  reply : Option String

def commit (run : Cell → List String) (notebook patched : Notebook) (reply : Option String) :
    StepResult :=
  { notebook := merge notebook run patched ++ (reply.map replyCell).toList,
    ran := changed notebook patched, reply := reply }

inductive AttemptResult where
  | retry (error : ReplyError)
  | providerError
  | committed (result : StepResult)

def runAttempt (limit : Nat) (run : Cell → List String) (notebook : Notebook) :
    Response → AttemptResult
  | .providerError => .providerError
  | .noStepCall => .retry .noStepCall
  | .malformedArguments => .retry .malformedArguments
  | .step none _ => .retry .invalidPatch
  | .step (some patched) reply =>
      if accepts limit notebook patched then .committed (commit run notebook patched reply)
      else .retry .overLimit

theorem invalid_patches_are_retried (limit : Nat) (run : Cell → List String)
    (notebook : Notebook) (reply : Option String) :
    runAttempt limit run notebook (.step none reply) = .retry .invalidPatch := rfl

theorem oversized_patches_are_retried (limit : Nat) (run : Cell → List String)
    (notebook patched : Notebook) (reply : Option String)
    (h : accepts limit notebook patched = false) :
    runAttempt limit run notebook (.step (some patched) reply) = .retry .overLimit := by
  simp [runAttempt, h]

theorem a_reply_is_kept_as_the_last_cell (limit : Nat) (run : Cell → List String)
    (notebook patched : Notebook) (text : String) (h : accepts limit notebook patched = true) :
    runAttempt limit run notebook (.step (some patched) (some text)) =
      .committed { notebook := merge notebook run patched ++ [replyCell text],
                   ran := changed notebook patched, reply := some text } := by
  simp [runAttempt, commit, h]

inductive StepStop where
  | committed (result : StepResult)
  | providerError
  | retriesExhausted
  | inputExhausted

/-- Try at most `maxRetries + 1` responses for a step. -/
def runStep (limit : Nat) (run : Cell → List String) (notebook : Notebook) (maxRetries : Nat) :
    List Response → StepStop
  | [] => .inputExhausted
  | response :: rest =>
      match runAttempt limit run notebook response with
      | .committed result => .committed result
      | .providerError => .providerError
      | .retry _ =>
          match maxRetries with
          | 0 => .retriesExhausted
          | retries + 1 => runStep limit run notebook retries rest

theorem provider_failures_are_not_retried (limit retries : Nat) (run : Cell → List String)
    (notebook : Notebook) (responses : List Response) :
    runStep limit run notebook retries (.providerError :: responses) = .providerError := by
  unfold runStep
  rfl

theorem retry_limit_is_max_retries_plus_one (limit : Nat) (run : Cell → List String)
    (notebook : Notebook) (responses : List Response) :
    runStep limit run notebook 0 (.noStepCall :: responses) = .retriesExhausted := rfl

/-! ## Events -/

inductive Event where
  | stepStart (notebook : Notebook)
  | validationError (error : ReplyError)
  | patch (ran : List Cell)
  | execution (ran : List Cell)
  | stepEnd (notebook : Notebook) (reply : Option String)

/-- Events after a patch is accepted: the patch, the run if any cell ran, the step end. -/
def committedTrace (result : StepResult) : List Event :=
  [.patch result.ran] ++ (if result.ran.isEmpty then [] else [.execution result.ran]) ++
    [.stepEnd result.notebook result.reply]

theorem nothing_ran_means_no_execution_event (result : StepResult) (h : result.ran = []) :
    committedTrace result = [.patch [], .stepEnd result.notebook result.reply] := by
  simp [committedTrace, h]

theorem committed_trace_starts_with_the_patch (result : StepResult) :
    (committedTrace result).take 1 = [.patch result.ran] := by
  simp [committedTrace]

/-! ## The outer loop -/

inductive RunStop where
  | replied
  | cancelled
  | stepLimit
  | providerError
  | retriesExhausted
  | inputExhausted

structure RunResult where
  notebook : Notebook
  steps : Nat
  stop : RunStop

/-- Each cancellation flag is checked before its step. -/
def runLoop (limit maxRetries : Nat) (run : Cell → List String) :
    Nat → Notebook → List Bool → List (List Response) → RunResult
  | 0, notebook, _, _ => { notebook, steps := 0, stop := .stepLimit }
  | _ + 1, notebook, true :: _, _ => { notebook, steps := 0, stop := .cancelled }
  | _ + 1, notebook, _, [] => { notebook, steps := 0, stop := .inputExhausted }
  | remaining + 1, notebook, flags, responses :: rest =>
      match runStep limit run notebook maxRetries responses with
      | .committed result =>
          if result.reply.isSome then
            { notebook := result.notebook, steps := 1, stop := .replied }
          else
            let next := runLoop limit maxRetries run remaining result.notebook flags.tail rest
            { next with steps := next.steps + 1 }
      | .providerError => { notebook, steps := 0, stop := .providerError }
      | .retriesExhausted => { notebook, steps := 0, stop := .retriesExhausted }
      | .inputExhausted => { notebook, steps := 0, stop := .inputExhausted }

theorem cancellation_preserves_the_notebook (limit retries : Nat) (run : Cell → List String)
    (notebook : Notebook) (responses : List (List Response)) :
    runLoop limit retries run 1 notebook [true] responses =
      { notebook, steps := 0, stop := .cancelled } := by
  simp [runLoop]

theorem step_limit_prevents_model_calls (limit retries : Nat) (run : Cell → List String)
    (notebook : Notebook) (cancelled : List Bool) (responses : List (List Response)) :
    runLoop limit retries run 0 notebook cancelled responses =
      { notebook, steps := 0, stop := .stepLimit } := rfl

theorem a_reply_ends_the_run (limit retries : Nat) (run : Cell → List String) (text : String) :
    runLoop limit retries run 1 [] [] [[.step (some []) (some text)]] =
      { notebook := [replyCell text], steps := 1, stop := .replied } := by
  cases retries <;> rfl

end RioAgent
