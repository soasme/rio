/- Behavioral models for rio.coding's state, tools, journal, and dispatch.

These are executable specifications of decisions made by the Python code, not
proofs about Python source. External I/O, hashes, and provider calls are inputs.
See README.md for the correspondence and limits.
-/
import RioAgent

namespace RioCoding

open RioAgent (Json Object)

/- coding_skill.py: all declared state fields are seeded. -/
def stateFields : List String :=
  ["goal", "plan", "findings", "files", "cwd", "environment", "blockers", "last_error", "scratch"]

def initialState (cwd goal : String) (environment : Object) : Object :=
  [("goal", .string goal), ("plan", .array []), ("findings", .object []),
   ("files", .object []), ("cwd", .string cwd), ("environment", .object environment),
   ("blockers", .array []), ("last_error", .null), ("scratch", .object [])]

theorem initial_state_has_all_fields (cwd goal : String) (environment : Object) :
    (initialState cwd goal environment).map Prod.fst = stateFields := by
  rfl

theorem initial_goal_and_cwd (cwd goal : String) (environment : Object) :
    RioAgent.lookup "goal" (initialState cwd goal environment) = some (.string goal) ∧
    RioAgent.lookup "cwd" (initialState cwd goal environment) = some (.string cwd) := by
  simp [initialState, RioAgent.lookup]

inductive PlanStatus where
  | pending | inProgress | done | blocked
deriving DecidableEq, Repr

structure PlanItem where
  status : PlanStatus
deriving DecidableEq, Repr

def planProgress (plan : List PlanItem) : Nat × Nat :=
  ((plan.filter (fun item => item.status == .done)).length, plan.length)

def canRespond (plan : List PlanItem) : Bool :=
  plan.all (fun item => item.status == .done || item.status == .blocked)

theorem empty_plan_can_respond : canRespond [] = true := by rfl

theorem pending_plan_blocks_respond (rest : List PlanItem) :
    canRespond ({ status := .pending } :: rest) = false := by
  simp [canRespond]

theorem completed_and_blocked_plan_can_respond (items : List PlanItem)
    (h : ∀ item ∈ items, item.status = .done ∨ item.status = .blocked) :
    canRespond items = true := by
  simp only [canRespond, List.all_eq_true]
  intro item hi
  rcases h item hi with hd | hb
  · simp [hd]
  · simp [hb]

/- file_context.py: existing files require a matching recorded hash before a write. -/
inductive FileAction where
  | read | write | edit | bash | respond
deriving DecidableEq, Repr

inductive Preflight where
  | proceed | readFirst | reread | finishPlan
deriving DecidableEq, Repr

def preflight (action : FileAction) (fileExists : Bool) (recorded actual : Option Nat)
    (plan : List PlanItem) : Preflight :=
  if action == .respond && !canRespond plan then .finishPlan
  else if (action == .write || action == .edit) && fileExists then
    match recorded, actual with
    | none, _ => .readFirst
    | some old, some current => if old == current then .proceed else .reread
    | some _, none => .reread
  else .proceed

theorem write_existing_unread_requires_read (hash : Option Nat) (plan : List PlanItem) :
    preflight .write true none hash plan = .readFirst := by
  simp [preflight]

theorem edit_stale_requires_reread (old current : Nat) (plan : List PlanItem)
    (h : old ≠ current) :
    preflight .edit true (some old) (some current) plan = .reread := by
  simp [preflight, h]

theorem fresh_edit_proceeds (hash : Nat) (plan : List PlanItem) :
    preflight .edit true (some hash) (some hash) plan = .proceed := by
  simp [preflight]

theorem new_file_does_not_require_hash (plan : List PlanItem) :
    preflight .write false none none plan = .proceed := by
  simp [preflight]

theorem respond_with_pending_work_rejected (rest : List PlanItem) :
    preflight .respond false none none ({ status := .pending } :: rest) = .finishPlan := by
  simp [preflight, pending_plan_blocks_respond]

inductive CacheResult where
  | full | metadataOnly | unchanged
deriving DecidableEq, Repr

/-- The runtime first tries slices, then metadata, then leaves state unchanged. -/
def cacheResult (fullSize metadataSize budget : Nat) : CacheResult :=
  if fullSize <= budget then .full
  else if metadataSize <= budget then .metadataOnly
  else .unchanged

theorem cache_falls_back_to_metadata (fullSize metadataSize budget : Nat)
    (hfull : budget < fullSize) (hmeta : metadataSize <= budget) :
    cacheResult fullSize metadataSize budget = .metadataOnly := by
  simp [cacheResult, Nat.not_le.mpr hfull, hmeta]

theorem cache_never_exceeds_budget (fullSize metadataSize budget : Nat) :
    cacheResult fullSize metadataSize budget = .full → fullSize <= budget := by
  by_cases h : fullSize <= budget
  · exact fun _ => h
  · by_cases hm : metadataSize <= budget <;> simp [cacheResult, h, hm]

/- tools.py: edit validation and terminal response decisions. -/
inductive EditError where
  | emptyOldText | notFound | duplicate | overlap | noChange
deriving DecidableEq, Repr

def validateEdit (emptyOld : Bool) (occurrences : Nat) (overlap changed : Bool) :
    Except EditError Unit :=
  if emptyOld then .error .emptyOldText
  else if occurrences == 0 then .error .notFound
  else if occurrences == 1 then
    if overlap then .error .overlap
    else if changed then .ok () else .error .noChange
  else .error .duplicate

theorem edit_requires_unique_match (n : Nat) (overlap changed : Bool)
    (h : 1 < n) : validateEdit false n overlap changed = .error .duplicate := by
  simp [validateEdit, Nat.ne_of_gt h, Nat.ne_of_gt (Nat.lt_trans (by decide : 0 < 1) h)]

theorem missing_text_rejected : validateEdit false 0 false true = .error .notFound := by rfl
theorem unchanged_edit_rejected : validateEdit false 1 false false = .error .noChange := by rfl
theorem unique_changed_edit_accepted : validateEdit false 1 false true = .ok () := by rfl

inductive ToolOutcome where
  | observation | termination | rejected
deriving DecidableEq, Repr

def toolOutcome (action : FileAction) (guard : Preflight) : ToolOutcome :=
  if guard != .proceed then .rejected
  else if action == .respond then .termination else .observation

theorem respond_is_terminal_after_plan_complete (plan : List PlanItem)
    (h : canRespond plan = true) :
    toolOutcome .respond (preflight .respond false none none plan) = .termination := by
  simp [toolOutcome, preflight, h]

/- session_store/tree.py: explicit leaf pointers and latest branch checkpoint. -/
structure JournalEntry where
  id : String
  parent : Option String
  leafPointer : Option String := none
  snapshot : Option Object := none

def latestLeaf : List JournalEntry → Option String
  | [] => none
  | entries =>
      match (entries.reverse.find? (fun entry => entry.leafPointer.isSome)) with
      | some pointer => pointer.leafPointer
      | none => entries.getLast?.map (·.id)

def latestSnapshot : List JournalEntry → Option Object
  | [] => none
  | entry :: rest =>
      match latestSnapshot rest with
      | some state => some state
      | none => entry.snapshot

def resumeState (path : List JournalEntry) : Object :=
  (latestSnapshot path).getD []

theorem empty_journal_has_empty_state : resumeState [] = [] := by rfl

private theorem latestSnapshot_append_checkpoint (history : List JournalEntry)
    (entry : JournalEntry) (state : Object) :
    latestSnapshot (history ++ [{ entry with snapshot := some state }]) = some state := by
  induction history with
  | nil => rfl
  | cons first rest ih => simp [latestSnapshot, ih]

theorem latest_checkpoint_wins (history : List JournalEntry) (entry : JournalEntry)
    (state : Object) :
    resumeState (history ++ [{ entry with snapshot := some state }]) = state := by
  simp [resumeState, latestSnapshot_append_checkpoint]

theorem metadata_after_checkpoint_does_not_change_state (history : List JournalEntry)
    (entry : JournalEntry) (state : Object) :
    resumeState (history ++ [{ entry with snapshot := some state },
      { id := "metadata", parent := some entry.id }]) = state := by
  have snapshot : latestSnapshot (history ++ [{ entry with snapshot := some state },
      { id := "metadata", parent := some entry.id }]) = some state := by
    induction history with
    | nil => rfl
    | cons first rest ih => simp [latestSnapshot, ih]
  simp [resumeState, snapshot]

/- commands.py and thinking.py: command routing and mode cycling. -/
inductive Route where
  | prompt | skillPrompt | command (name : String) | unknownCommand
deriving DecidableEq, Repr

inductive ParsedInput where
  | text | skill (name : String) | slash (name : String)

def route (known : List String) : ParsedInput → Route
  | .text => .prompt
  | .skill _ => .skillPrompt
  | .slash name => if known.contains name then .command name else .unknownCommand

theorem ordinary_text_is_prompt (known : List String) : route known .text = .prompt := by rfl

theorem skill_command_is_prompt (known : List String) (name : String) :
    route known (.skill name) = .skillPrompt := by rfl

inductive Thinking where
  | off | minimal | low | medium | high | xhigh | max
deriving DecidableEq, Repr

def thinkingCycle : List Thinking :=
  [.off, .minimal, .low, .medium, .high, .xhigh, .max]

def nextThinking (available : List Thinking) (current : Thinking) : Thinking :=
  match available with
  | [] => .medium
  | first :: _ =>
      match (available.dropWhile (· != current)).drop 1 with
      | next :: _ => next
      | [] => first

theorem empty_thinking_set_uses_default (current : Thinking) :
    nextThinking [] current = .medium := by rfl

theorem thinking_wraps : nextThinking thinkingCycle .max = .off := by rfl

end RioCoding
