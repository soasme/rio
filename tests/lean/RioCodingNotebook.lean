/- A formal control-flow model of `rio.coding.loop.run_notebook_loop`.

The context is a notebook: a list of cells. The model abstracts provider I/O,
JSON Patch application, nbformat validation, and kernel execution. A patch is
given as its result (`none` when it fails to apply or leaves an invalid
notebook), and `kernel.run` gives the outputs of a cell that runs. Notebook size is
total text length rather than a token estimate.

Cells run in one live kernel, so a cell that did not change never runs again.
It retains the loop's decisions: which cells run, which outputs are kept, the
stop after a failing cell, the size gate on patches, retries for unusable replies, provider failures, event
order, the reply that ends a run, cancellation, and the step limit.
-/
namespace RioCodingNotebook

structure Cell where
  id : Nat
  code : Bool
  source : String
  outputs : List String
  /-- The id of the kernel that last ran the cell. -/
  kernel : Option Nat := none
  /-- Names the cell bound when it last ran, and names its source reads. -/
  defines : List String := []
  reads : List String := []
deriving DecidableEq, Repr

/-- The live kernel: its id, the outputs a cell produces when it runs, and the
names the run binds (recorded by the kernel, not read from the source). -/
structure Kernel where
  id : Nat
  run : Cell → List String
  binds : Cell → List String

abbrev Notebook := List Cell

def cellSize (cell : Cell) : Nat := cell.source.length + (cell.outputs.map String.length).sum

def size (notebook : Notebook) : Nat := (notebook.map cellSize).sum

/-! ## Which cells run -/

def sourceOf (before : Notebook) (id : Nat) : Option String :=
  (before.find? (·.id == id)).map (·.source)

/-- The patch removed the cell's kernel stamp: the way to run it again unedited. -/
def stampRemoved (before : Notebook) (cell : Cell) : Bool :=
  ((before.find? (·.id == cell.id)).bind (·.kernel)).isSome && cell.kernel.isNone

/-- A code cell runs when it is new, its source changed, or its stamp was removed. -/
def isChanged (before : Notebook) (cell : Cell) : Bool :=
  cell.code && (sourceOf before cell.id != some cell.source || stampRemoved before cell)

def changed (before after : Notebook) : List Cell := after.filter (isChanged before)

theorem markdown_cells_never_run (before after : Notebook) (cell : Cell)
    (h : cell ∈ changed before after) : cell.code = true := by
  simp [changed, isChanged, List.mem_filter] at h
  exact h.2.1

theorem same_source_does_not_run (before : Notebook) (cell : Cell)
    (h : sourceOf before cell.id = some cell.source) (stamped : stampRemoved before cell = false) :
    isChanged before cell = false := by
  simp [isChanged, h, stamped]

theorem edited_outputs_do_not_run (before : Notebook) (cell : Cell) (outputs : List String)
    (h : sourceOf before cell.id = some cell.source) (stamped : stampRemoved before cell = false) :
    isChanged before { cell with outputs := outputs } = false := by
  simp [isChanged, stampRemoved] at stamped ⊢
  simp [h]
  exact fun _ => stamped

theorem removing_the_stamp_runs_the_cell (before : Notebook) (old cell : Cell) (k : Nat)
    (found : before.find? (·.id == cell.id) = some old) (stamped : old.kernel = some k)
    (code : cell.code = true) (removed : cell.kernel = none) : isChanged before cell = true := by
  simp [isChanged, stampRemoved, found, stamped, code, removed]

/-! ## Stale cells -/

/-- A code cell that ran in another kernel: its variables, files, and processes may be gone. -/
def isStale (current : Nat) (cell : Cell) : Bool :=
  cell.code && cell.kernel.isSome && cell.kernel != some current

theorem a_new_kernel_makes_cells_that_ran_stale (old new : Nat) (cell : Cell)
    (code : cell.code = true) (ran : cell.kernel = some old) (fresh : old ≠ new) :
    isStale new cell = true := by
  simp [isStale, code, ran, fresh]

/-! ## Reading variables from stale cells -/

def staleDefines (current : Nat) (others : List Cell) (name : String) : Bool :=
  others.any (fun other => isStale current other && other.defines.contains name)

def liveDefines (current : Nat) (others : List Cell) (name : String) : Bool :=
  others.any (fun other => other.kernel == some current && other.defines.contains name)

/-- A patch is rejected when a cell about to run reads a name only stale cells define. -/
def readsStale (current : Nat) (others : List Cell) (cell : Cell) : Bool :=
  cell.reads.any fun name =>
    !cell.defines.contains name && staleDefines current others name &&
      !liveDefines current others name

theorem leaving_cells_stale_is_allowed (current : Nat) (others : List Cell) (cell : Cell)
    (h : cell.reads = []) : readsStale current others cell = false := by
  simp [readsStale, h]

theorem reading_a_stale_only_name_is_rejected (old current : Nat) (name : String)
    (fresh : old ≠ current) :
    readsStale current
      [{ id := 0, code := true, source := "", outputs := [], kernel := some old,
         defines := [name] }]
      { id := 1, code := true, source := "", outputs := [], reads := [name] } = true := by
  simp [readsStale, staleDefines, liveDefines, isStale, fresh]

theorem a_live_definition_satisfies_the_read (old current : Nat) (name : String) :
    readsStale current
      [{ id := 0, code := true, source := "", outputs := [], kernel := some old,
         defines := [name] },
       { id := 2, code := true, source := "", outputs := [], kernel := some current,
         defines := [name] }]
      { id := 1, code := true, source := "", outputs := [], reads := [name] } = false := by
  simp [readsStale, liveDefines]

theorem redefining_the_name_satisfies_the_read (current : Nat) (others : List Cell)
    (name : String) :
    readsStale current others
      { id := 1, code := true, source := "", outputs := [], defines := [name],
        reads := [name] } = false := by
  simp [readsStale]

/-- In a new kernel, each name only stale cells defined is bound to a placeholder
that fails when used, so reads the static check misses still cannot see a value. -/
def placeholders (current : Nat) (cells : List Cell) (name : String) : Bool :=
  staleDefines current cells name && !liveDefines current cells name

theorem a_rejected_read_is_a_placeholder (current : Nat) (others : List Cell) (cell : Cell)
    (name : String) (h : name ∈ cell.reads) (fresh : cell.defines.contains name = false)
    (bad : placeholders current others name = true) : readsStale current others cell = true := by
  simp [placeholders] at bad
  simp [readsStale, List.any_eq_true]
  refine ⟨name, h, ?_⟩
  simp_all

/-! ## Merging outputs -/

/-- Changed cells take the outputs of their run; every other cell keeps its own. -/
def mergeCell (before : Notebook) (kernel : Kernel) (cell : Cell) : Cell :=
  if isChanged before cell then
    { cell with outputs := kernel.run cell, kernel := some kernel.id,
                defines := kernel.binds cell }
  else cell

def merge (before : Notebook) (kernel : Kernel) (after : Notebook) : Notebook :=
  after.map (mergeCell before kernel)

theorem unchanged_cells_keep_their_outputs (before : Notebook) (kernel : Kernel)
    (cell : Cell) (h : isChanged before cell = false) : mergeCell before kernel cell = cell := by
  simp [mergeCell, h]

theorem changed_cells_get_new_outputs (before : Notebook) (kernel : Kernel)
    (cell : Cell) (h : isChanged before cell = true) :
    (mergeCell before kernel cell).outputs = kernel.run cell := by
  simp [mergeCell, h]

theorem merge_keeps_every_cell (before : Notebook) (kernel : Kernel)
    (after : Notebook) : ((merge before kernel after).map (·.id)) = after.map (·.id) := by
  simp [merge, mergeCell, Function.comp_def]
  intro cell _
  split <;> rfl

theorem cells_that_ran_record_what_they_bound (before : Notebook) (kernel : Kernel)
    (cell : Cell) (h : isChanged before cell = true) :
    (mergeCell before kernel cell).defines = kernel.binds cell := by
  simp [mergeCell, h]

theorem cells_that_ran_are_not_stale (before : Notebook) (kernel : Kernel) (cell : Cell)
    (h : isChanged before cell = true) : isStale kernel.id (mergeCell before kernel cell) = false := by
  simp [mergeCell, h, isStale]

/-! ## Running changed cells in order -/

/-- Changed cells run in order; after the first failing cell, the rest get no outputs. -/
def runInOrder (fails : Cell → Bool) (kernel : Kernel) : List Cell → List Cell
  | [] => []
  | cell :: rest =>
      if fails cell then
        { cell with outputs := kernel.run cell, kernel := some kernel.id,
                    defines := kernel.binds cell } ::
          rest.map (fun later => { later with outputs := [], kernel := none, defines := [] })
      else { cell with outputs := kernel.run cell, kernel := some kernel.id,
                       defines := kernel.binds cell } ::
        runInOrder fails kernel rest

theorem cells_after_a_failure_get_no_outputs (fails : Cell → Bool) (kernel : Kernel)
    (cell : Cell) (rest : List Cell) (h : fails cell = true) :
    (runInOrder fails kernel (cell :: rest)).tail.all (·.outputs.isEmpty) := by
  simp [runInOrder, h]

theorem run_in_order_keeps_every_cell (fails : Cell → Bool) (kernel : Kernel)
    (cells : List Cell) : (runInOrder fails kernel cells).map (·.id) = cells.map (·.id) := by
  induction cells with
  | nil => rfl
  | cons cell rest ih =>
      by_cases h : fails cell
      · simp [runInOrder, h, Function.comp_def]
      · simp [runInOrder, h, ih]

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

def commit (kernel : Kernel) (notebook patched : Notebook) (reply : Option String) :
    StepResult :=
  { notebook := merge notebook kernel patched ++ (reply.map replyCell).toList,
    ran := changed notebook patched, reply := reply }

def blank (text : String) : Bool := text.toList.all Char.isWhitespace

/-- A blank reply is no answer. -/
def answer (reply : Option String) : Option String := reply.filter (!blank ·)

theorem answer_keeps_text (text : String) (h : blank text = false) :
    answer (some text) = some text := by
  simp [answer, Option.filter, h]

inductive AttemptResult where
  | retry (error : ReplyError)
  | providerError
  | committed (result : StepResult)

def runAttempt (limit : Nat) (kernel : Kernel) (notebook : Notebook) :
    Response → AttemptResult
  | .providerError => .providerError
  | .noStepCall => .retry .noStepCall
  | .malformedArguments => .retry .malformedArguments
  | .step none _ => .retry .invalidPatch
  | .step (some patched) reply =>
      if accepts limit notebook patched then
        .committed (commit kernel notebook patched (answer reply))
      else .retry .overLimit

theorem invalid_patches_are_retried (limit : Nat) (kernel : Kernel)
    (notebook : Notebook) (reply : Option String) :
    runAttempt limit kernel notebook (.step none reply) = .retry .invalidPatch := rfl

theorem oversized_patches_are_retried (limit : Nat) (kernel : Kernel)
    (notebook patched : Notebook) (reply : Option String)
    (h : accepts limit notebook patched = false) :
    runAttempt limit kernel notebook (.step (some patched) reply) = .retry .overLimit := by
  simp [runAttempt, h]

theorem a_reply_is_kept_as_the_last_cell (limit : Nat) (kernel : Kernel)
    (notebook patched : Notebook) (text : String) (h : accepts limit notebook patched = true)
    (hText : blank text = false) :
    runAttempt limit kernel notebook (.step (some patched) (some text)) =
      .committed { notebook := merge notebook kernel patched ++ [replyCell text],
                   ran := changed notebook patched, reply := some text } := by
  simp [runAttempt, commit, answer_keeps_text text hText, h]

theorem a_blank_reply_is_no_reply (limit : Nat) (kernel : Kernel)
    (notebook patched : Notebook) (h : accepts limit notebook patched = true) :
    runAttempt limit kernel notebook (.step (some patched) (some "")) =
      .committed { notebook := merge notebook kernel patched,
                   ran := changed notebook patched, reply := none } := by
  simp [runAttempt, commit, answer, blank, h]

inductive StepStop where
  | committed (result : StepResult)
  | providerError
  | retriesExhausted
  | inputExhausted

/-- Try at most `maxRetries + 1` responses for a step. -/
def runStep (limit : Nat) (kernel : Kernel) (notebook : Notebook) (maxRetries : Nat) :
    List Response → StepStop
  | [] => .inputExhausted
  | response :: rest =>
      match runAttempt limit kernel notebook response with
      | .committed result => .committed result
      | .providerError => .providerError
      | .retry _ =>
          match maxRetries with
          | 0 => .retriesExhausted
          | retries + 1 => runStep limit kernel notebook retries rest

theorem provider_failures_are_not_retried (limit retries : Nat) (kernel : Kernel)
    (notebook : Notebook) (responses : List Response) :
    runStep limit kernel notebook retries (.providerError :: responses) = .providerError := by
  unfold runStep
  rfl

theorem retry_limit_is_max_retries_plus_one (limit : Nat) (kernel : Kernel)
    (notebook : Notebook) (responses : List Response) :
    runStep limit kernel notebook 0 (.noStepCall :: responses) = .retriesExhausted := rfl

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
def runLoop (limit maxRetries : Nat) (kernel : Kernel) :
    Nat → Notebook → List Bool → List (List Response) → RunResult
  | 0, notebook, _, _ => { notebook, steps := 0, stop := .stepLimit }
  | _ + 1, notebook, true :: _, _ => { notebook, steps := 0, stop := .cancelled }
  | _ + 1, notebook, _, [] => { notebook, steps := 0, stop := .inputExhausted }
  | remaining + 1, notebook, flags, responses :: rest =>
      match runStep limit kernel notebook maxRetries responses with
      | .committed result =>
          if result.reply.isSome then
            { notebook := result.notebook, steps := 1, stop := .replied }
          else
            let next := runLoop limit maxRetries kernel remaining result.notebook flags.tail rest
            { next with steps := next.steps + 1 }
      | .providerError => { notebook, steps := 0, stop := .providerError }
      | .retriesExhausted => { notebook, steps := 0, stop := .retriesExhausted }
      | .inputExhausted => { notebook, steps := 0, stop := .inputExhausted }

theorem cancellation_preserves_the_notebook (limit retries : Nat) (kernel : Kernel)
    (notebook : Notebook) (responses : List (List Response)) :
    runLoop limit retries kernel 1 notebook [true] responses =
      { notebook, steps := 0, stop := .cancelled } := by
  simp [runLoop]

theorem step_limit_prevents_model_calls (limit retries : Nat) (kernel : Kernel)
    (notebook : Notebook) (cancelled : List Bool) (responses : List (List Response)) :
    runLoop limit retries kernel 0 notebook cancelled responses =
      { notebook, steps := 0, stop := .stepLimit } := rfl

theorem a_reply_ends_the_run (limit retries : Nat) (kernel : Kernel) (text : String)
    (hText : blank text = false) :
    runLoop limit retries kernel 1 [] [] [[.step (some []) (some text)]] =
      { notebook := [replyCell text], steps := 1, stop := .replied } := by
  cases retries <;> simp [runLoop, runStep, runAttempt, commit, answer_keeps_text text hText, accepts, merge, changed, size]

end RioCodingNotebook
